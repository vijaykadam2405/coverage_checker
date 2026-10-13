"""Commercial CAPTCHA solver abstraction.

Supported providers: capsolver, 2captcha, anticaptcha.
Usage:
    solver = CaptchaSolver.from_env()
    result = solver.solve_hcaptcha(sitekey="...", page_url="...")

The module is defensive: missing API key, timeout, or network error returns
CaptchaResult(solved=False) so callers can fall back to NEEDS_NETWORK_PATH.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("captcha_solver")


def _env_float(k: str, d: float) -> float:
    try:
        return float(os.environ.get(k, str(d)))
    except ValueError:
        return d


def _env_int(k: str, d: int) -> int:
    try:
        return int(os.environ.get(k, str(d)))
    except ValueError:
        return d


@dataclass
class CaptchaResult:
    solved: bool = False
    token: str | None = None
    method: str | None = None          # e.g. capsolver, 2captcha, anticaptcha
    cost_usd: float | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class CaptchaSolver:
    """Base solver. Use from_env() to pick the provider."""

    def __init__(self, api_key: str | None = None, timeout_s: float | None = None,
                 max_cost_usd: float | None = None, daily_budget_usd: float | None = None) -> None:
        self.api_key = (api_key or "").strip()
        self.timeout_s = timeout_s if timeout_s is not None else _env_float("CAPTCHA_TIMEOUT_S", 120.0)
        self.max_cost_usd = max_cost_usd if max_cost_usd is not None else _env_float("CAPTCHA_MAX_COST_USD", 0.05)
        self.daily_budget_usd = daily_budget_usd if daily_budget_usd is not None else _env_float("CAPTCHA_DAILY_BUDGET_USD", 10.0)
        self._daily_spent: float = 0.0

    def _log_spend(self, cost: float | None) -> None:
        if cost:
            self._daily_spent += cost

    def _budget_ok(self) -> bool:
        if self.daily_budget_usd is None:
            return True
        if self._daily_spent >= self.daily_budget_usd:
            log.warning("daily captcha budget exhausted: spent %.4f / %.4f USD",
                        self._daily_spent, self.daily_budget_usd)
            return False
        return True

    def solve_recaptcha_v2(self, sitekey: str, page_url: str, invisible: bool = False,
                           enterprise: bool = False, proxy: str | None = None) -> CaptchaResult:
        raise NotImplementedError

    def solve_recaptcha_v3(self, sitekey: str, page_url: str, action: str | None = None,
                           min_score: float | None = None, enterprise: bool = False) -> CaptchaResult:
        raise NotImplementedError

    def solve_hcaptcha(self, sitekey: str, page_url: str, invisible: bool = False,
                       proxy: str | None = None) -> CaptchaResult:
        raise NotImplementedError

    def solve_turnstile(self, sitekey: str, page_url: str, action: str | None = None,
                        cdata: str | None = None) -> CaptchaResult:
        raise NotImplementedError

    def solve_datadome(self, captcha_url: str, page_url: str, proxy: str | None = None) -> CaptchaResult:
        raise NotImplementedError

    @classmethod
    def from_env(cls) -> "CaptchaSolver":
        provider = os.environ.get("CAPTCHA_PROVIDER", "").strip().lower()
        key = os.environ.get("CAPTCHA_API_KEY", "").strip()
        if not provider or not key:
            log.debug("no commercial captcha solver configured (provider=%r key_set=%s)", provider, bool(key))
            return _NullSolver()
        if provider == "capsolver":
            return CapSolver(key)
        if provider == "2captcha":
            return TwoCaptcha(key)
        if provider == "anticaptcha":
            return AntiCaptcha(key)
        log.warning("unknown CAPTCHA_PROVIDER=%r; using null solver", provider)
        return _NullSolver()


class _NullSolver(CaptchaSolver):
    """No-op solver used when no provider/key is configured."""

    def _solve(self, *_, **__) -> CaptchaResult:
        return CaptchaResult(solved=False, error="no commercial captcha solver configured")

    solve_recaptcha_v2 = _solve
    solve_recaptcha_v3 = _solve
    solve_hcaptcha = _solve
    solve_turnstile = _solve
    solve_datadome = _solve


class _PollingSolver(CaptchaSolver):
    """Shared polling logic used by CapSolver, 2captcha, and AntiCaptcha."""

    def __init__(self, api_key: str, base_url: str, create_path: str, result_path: str,
                 token_key: str, error_key: str, status_key: str | None = None,
                 ready_status: str = "ready", **kw) -> None:
        super().__init__(api_key, **kw)
        self.base_url = base_url.rstrip("/")
        self.create_path = create_path
        self.result_path = result_path
        self.token_key = token_key
        self.error_key = error_key
        self.status_key = status_key
        self.ready_status = ready_status

    def _create_task(self, payload: dict[str, Any]) -> str | None:
        payload = {k: v for k, v in payload.items() if v is not None}
        try:
            r = httpx.post(f"{self.base_url}{self.create_path}", json=payload,
                           timeout=min(30.0, self.timeout_s), follow_redirects=True)
            r.raise_for_status()
            data = r.json()
            if data.get(self.error_key):
                log.warning("solver create failed: %s", data.get(self.error_key))
                return None
            return data.get("taskId") or data.get("id") or data.get("task_id")
        except Exception as e:
            log.warning("solver create error: %s", e)
            return None

    def _poll_result(self, task_id: str) -> CaptchaResult:
        start = time.time()
        interval = 2.0
        while time.time() - start < self.timeout_s:
            try:
                r = httpx.post(f"{self.base_url}{self.result_path}",
                               json={"clientKey": self.api_key, "taskId": task_id},
                               timeout=30.0, follow_redirects=True)
                r.raise_for_status()
                data = r.json()
                if data.get(self.error_key):
                    return CaptchaResult(solved=False, error=str(data.get(self.error_key)))
                if self.status_key and data.get(self.status_key) == self.ready_status:
                    token = self._extract_token(data)
                    cost = self._extract_cost(data)
                    if token:
                        self._log_spend(cost)
                        return CaptchaResult(solved=True, token=token, method=self.method(), cost_usd=cost)
                # Provider-specific: some return token directly without status key
                token = self._extract_token(data)
                if token:
                    cost = self._extract_cost(data)
                    self._log_spend(cost)
                    return CaptchaResult(solved=True, token=token, method=self.method(), cost_usd=cost)
            except Exception as e:
                log.warning("solver poll error: %s", e)
            time.sleep(interval)
            interval = min(interval + 1.0, 10.0)
        return CaptchaResult(solved=False, error="solver timeout")

    def _extract_token(self, data: dict[str, Any]) -> str | None:
        val = data.get(self.token_key)
        if val and isinstance(val, str):
            return val
        # some providers nest token under solution.gRecaptchaResponse or solution.token
        sol = data.get("solution") or {}
        for k in ("gRecaptchaResponse", "token", "gRecaptchaResponse", "response"):
            v = sol.get(k)
            if v and isinstance(v, str):
                return v
        return None

    def _extract_cost(self, data: dict[str, Any]) -> float | None:
        cost = data.get("cost") or (data.get("solution") or {}).get("cost")
        if isinstance(cost, (int, float)):
            return float(cost)
        return None

    def method(self) -> str:
        return "unknown"


class CapSolver(_PollingSolver):
    """CapSolver implementation. Docs: https://docs.capsolver.com/"""

    def __init__(self, api_key: str, **kw) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://api.capsolver.com",
            create_path="/createTask",
            result_path="/getTaskResult",
            token_key="gRecaptchaResponse",
            error_key="errorId",
            status_key="status",
            ready_status="ready",
            **kw,
        )

    def method(self) -> str:
        return "capsolver"

    def _build_task(self, task_type: str, **extra) -> dict[str, Any]:
        return {
            "clientKey": self.api_key,
            "task": {"type": task_type, **extra},
        }

    def _create_task(self, payload: dict[str, Any]) -> str | None:
        # CapSolver uses task object wrapped in payload
        return super()._create_task(payload)

    def solve_recaptcha_v2(self, sitekey: str, page_url: str, invisible: bool = False,
                           enterprise: bool = False, proxy: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        task_type = "ReCaptchaV2EnterpriseTask" if enterprise else "ReCaptchaV2TaskProxyLess"
        payload = self._build_task(task_type, websiteURL=page_url, websiteKey=sitekey, isInvisible=invisible)
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create CapSolver task")
        return self._poll_result(task_id)

    def solve_recaptcha_v3(self, sitekey: str, page_url: str, action: str | None = None,
                           min_score: float | None = None, enterprise: bool = False) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        task_type = "ReCaptchaV3EnterpriseTask" if enterprise else "ReCaptchaV3TaskProxyLess"
        payload = self._build_task(task_type, websiteURL=page_url, websiteKey=sitekey, pageAction=action)
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create CapSolver task")
        return self._poll_result(task_id)

    def solve_hcaptcha(self, sitekey: str, page_url: str, invisible: bool = False,
                       proxy: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = self._build_task("HCaptchaTaskProxyLess", websiteURL=page_url, websiteKey=sitekey, isInvisible=invisible)
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create CapSolver task")
        return self._poll_result(task_id)

    def solve_turnstile(self, sitekey: str, page_url: str, action: str | None = None,
                        cdata: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = self._build_task("AntiTurnstileTaskProxyLess", websiteURL=page_url, websiteKey=sitekey)
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create CapSolver task")
        return self._poll_result(task_id)

    def solve_datadome(self, captcha_url: str, page_url: str, proxy: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = self._build_task("AntiDatadomeTaskProxyLess", websiteURL=page_url, captchaURL=captcha_url)
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create CapSolver task")
        return self._poll_result(task_id)


class TwoCaptcha(_PollingSolver):
    """2captcha implementation."""

    def __init__(self, api_key: str, **kw) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://api.2captcha.com",
            create_path="/in.php",
            result_path="/res.php",
            token_key="request",
            error_key="error",
            **kw,
        )

    def method(self) -> str:
        return "2captcha"

    def _create_task(self, payload: dict[str, Any]) -> str | None:
        # 2captcha uses form-encoded GET/POST with key=...&method=...&... and returns OK|CAPTCHA_ID or ERR
        try:
            r = httpx.post(f"{self.base_url}{self.create_path}", data=payload,
                           timeout=min(30.0, self.timeout_s), follow_redirects=True)
            r.raise_for_status()
            text = r.text.strip()
            if text.startswith("OK|"):
                return text[3:]
            log.warning("2captcha create failed: %s", text)
        except Exception as e:
            log.warning("2captcha create error: %s", e)
        return None

    def _poll_result(self, task_id: str) -> CaptchaResult:
        start = time.time()
        interval = 5.0
        while time.time() - start < self.timeout_s:
            try:
                r = httpx.get(f"{self.base_url}{self.result_path}", params={
                    "key": self.api_key,
                    "action": "get",
                    "id": task_id,
                }, timeout=30.0, follow_redirects=True)
                r.raise_for_status()
                text = r.text.strip()
                if text.startswith("CAPTCHA_NOT_READY"):
                    pass
                elif text.startswith("OK|"):
                    return CaptchaResult(solved=True, token=text[3:], method=self.method())
                elif text.startswith("ERROR_"):
                    return CaptchaResult(solved=False, error=text)
                else:
                    log.warning("2captcha unexpected response: %s", text)
            except Exception as e:
                log.warning("2captcha poll error: %s", e)
            time.sleep(interval)
            interval = min(interval + 2.0, 15.0)
        return CaptchaResult(solved=False, error="2captcha timeout")

    def solve_recaptcha_v2(self, sitekey: str, page_url: str, invisible: bool = False,
                           enterprise: bool = False, proxy: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = {
            "key": self.api_key,
            "method": "userrecaptcha",
            "googlekey": sitekey,
            "pageurl": page_url,
            "invisible": int(invisible),
            "enterprise": int(enterprise),
            "json": 1,
        }
        if proxy:
            payload["proxy"] = proxy
            payload["proxytype"] = "HTTP"
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create 2captcha task")
        return self._poll_result(task_id)

    def solve_hcaptcha(self, sitekey: str, page_url: str, invisible: bool = False,
                       proxy: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = {
            "key": self.api_key,
            "method": "hcaptcha",
            "sitekey": sitekey,
            "pageurl": page_url,
            "invisible": int(invisible),
            "json": 1,
        }
        if proxy:
            payload["proxy"] = proxy
            payload["proxytype"] = "HTTP"
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create 2captcha task")
        return self._poll_result(task_id)

    def solve_turnstile(self, sitekey: str, page_url: str, action: str | None = None,
                        cdata: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = {
            "key": self.api_key,
            "method": "turnstile",
            "sitekey": sitekey,
            "pageurl": page_url,
            "json": 1,
        }
        if action:
            payload["action"] = action
        if cdata:
            payload["cdata"] = cdata
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create 2captcha task")
        return self._poll_result(task_id)

    def solve_datadome(self, captcha_url: str, page_url: str, proxy: str | None = None) -> CaptchaResult:
        # 2captcha does not have a dedicated DataDome task; fall through to generic captcha.
        return CaptchaResult(solved=False, error="2captcha DataDome not supported")


class AntiCaptcha(_PollingSolver):
    """Anti-Captcha implementation."""

    def __init__(self, api_key: str, **kw) -> None:
        super().__init__(
            api_key=api_key,
            base_url="https://api.anti-captcha.com",
            create_path="/createTask",
            result_path="/getTaskResult",
            token_key="gRecaptchaResponse",
            error_key="errorCode",
            status_key="status",
            ready_status="ready",
            **kw,
        )

    def method(self) -> str:
        return "anticaptcha"

    def solve_recaptcha_v2(self, sitekey: str, page_url: str, invisible: bool = False,
                           enterprise: bool = False, proxy: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "RecaptchaV2TaskProxyless",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "isInvisible": invisible,
            },
        }
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create Anti-Captcha task")
        return self._poll_result(task_id)

    def solve_hcaptcha(self, sitekey: str, page_url: str, invisible: bool = False,
                       proxy: str | None = None) -> CaptchaResult:
        if not self._budget_ok():
            return CaptchaResult(solved=False, error="daily budget exhausted")
        payload = {
            "clientKey": self.api_key,
            "task": {
                "type": "HCaptchaTaskProxyless",
                "websiteURL": page_url,
                "websiteKey": sitekey,
                "isInvisible": invisible,
            },
        }
        task_id = self._create_task(payload)
        if not task_id:
            return CaptchaResult(solved=False, error="failed to create Anti-Captcha task")
        return self._poll_result(task_id)

    def solve_turnstile(self, sitekey: str, page_url: str, action: str | None = None,
                        cdata: str | None = None) -> CaptchaResult:
        # Anti-Captcha does not currently support Turnstile; return not supported.
        return CaptchaResult(solved=False, error="Anti-Captcha Turnstile not supported")

    def solve_datadome(self, captcha_url: str, page_url: str, proxy: str | None = None) -> CaptchaResult:
        return CaptchaResult(solved=False, error="Anti-Captcha DataDome not supported")
