# Keep C4 alive: the host solver, and the reverse tunnel the data centre reaches it through.
#
#   powershell -ExecutionPolicy Bypass -File scripts\c4_up.ps1
#
# WHY THIS EXISTS
# ---------------
# C4 is the only fetch tier that clears a Cloudflare-interactive wall, and it cannot run at the
# data centre: that box is a VMware guest whose hypervisor exposes no 3D, so Chrome there reports
# `ANGLE (Mesa, llvmpipe)` even with /dev/dri passed through — the exact fingerprint an interactive
# challenge refuses (measured 2026-08-02, see docker-compose.solver.yml). This workstation has the
# only real GPU in the system, so C4 lives here and is reached over a tunnel.
#
# A tunnel is a thing that dies quietly. It died three times in one day, and every failure was
# invisible: the crawler's hostsolver client swallows a failed hop on purpose ("record the wall and
# move on"), so a dead C4 looks exactly like a crawl that met no walls. Worse, one of those deaths
# sent traffic to a DIFFERENT LOCAL SERVICE holding the same port, which answered 200 happily and
# cost most of a day's diagnosis.
#
# So this script does three things a person otherwise has to remember:
#   1. starts the solver if it is not already listening;
#   2. holds the tunnel open and RECONNECTS when it drops, with keepalives tuned so a dead link is
#      noticed in ~45s rather than hanging forever in a half-open socket;
#   3. refuses to run if the local port is already taken by something that is not the solver.
#
# ASCII only, and no `&&`/ternaries: PowerShell 5.1 reads .ps1 as ANSI and has neither operator.

param(
  # No baked-in target. The data-centre host / user / port come from the environment, so this
  # public script carries no address to attack; unset means it fails loudly at connect time rather
  # than tunnelling somewhere unexpected. Set DATA_CENTRE_HOST / _USER / _PORT before running.
  [string]$RemoteHost = "$env:DATA_CENTRE_HOST",
  [string]$RemoteUser = "$env:DATA_CENTRE_USER",
  [int]   $RemotePort = $(if ($env:DATA_CENTRE_PORT) { [int]$env:DATA_CENTRE_PORT } else { 22 }),
  [string]$KeyPath    = "$env:USERPROFILE\.ssh\mallory_deploy",
  # The port the solver listens on HERE.
  [int]   $SolverPort = 9378,
  # The port the forward binds THERE. These were one value until it bit: if an ssh client dies
  # without a clean disconnect, sshd can keep the remote listener bound, and then every reconnect
  # fails with "remote port forwarding failed for listen port N" forever. The stale listener still
  # ACCEPTS TCP - it just never answers - so anything testing mere connectivity calls it healthy.
  # Reaping it needs root (it is held by the sshd [priv] process) and there is no passwordless
  # sudo on that box, so the escape hatch is to move the remote port. Keep this in step with
  # C4_TUNNEL_PORT in the data centre .env, which is what the hostsolver-relay forwards to.
  #
  # 9380 -> 9381 because the live deployment ALREADY MOVED and this default did not follow it: the
  # supervisor was running as `-TunnelPort 9381` and the relay forwards to 127.0.0.1:9381, so
  # anyone restarting this script the documented way (no flag) would have bound 9380, left the
  # relay pointing at nothing, and taken C4 down silently - the exact failure the flag exists to
  # escape. The default must BE the deployed value, not the historical one.
  # 2026-09-16: the deployment moved AGAIN (9381 -> 9382); relay socat + C4_TUNNEL_PORT in .env
  # both forward to 9382, so the default follows. Binding 9381 here left the relay pointing at
  # nothing and C4 silently down - the exact drift this flag exists to escape.
  [int]   $TunnelPort = 9382
)

$ErrorActionPreference = "Stop"
$repo   = Split-Path -Parent $PSScriptRoot
$solver = Join-Path $repo "crawler\scripts\host_solver.py"

# The solver needs the uc venv (seleniumbase + a real Chrome), not the crawler venv.
$python = Join-Path (Split-Path -Parent $repo) "cralwer\ucvenv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

function Test-Solver {
  # Identity, not just liveness. A port answering is not proof it is OUR service — that mistake is
  # the reason this whole script exists, so the one check it makes is the one that would have
  # caught it: host_solver's /health uniquely returns browser_open.
  try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$SolverPort/health" -TimeoutSec 5 -UseBasicParsing
    return ($r.Content -match "browser_open")
  } catch { return $false }
}

function Test-PortBusy {
  $c = Get-NetTCPConnection -LocalPort $SolverPort -State Listen -ErrorAction SilentlyContinue
  return ($null -ne $c)
}

Write-Host "[c4] solver script : $solver"
Write-Host "[c4] python        : $python"
Write-Host "[c4] tunnel        : $RemoteUser@$RemoteHost`:$RemotePort  -R ${TunnelPort}:127.0.0.1:${SolverPort}"
Write-Host ""

while ($true) {
  # --- 1. the solver -----------------------------------------------------------------------
  if (-not (Test-Solver)) {
    if (Test-PortBusy) {
      # Something else holds the port. Starting a second solver would bind-fail and leave the
      # imposter serving the tunnel, which is the failure this script must never create.
      Write-Host "[c4] ERROR: port $SolverPort is held by something that is NOT the host solver."
      Write-Host "[c4]        Free it before continuing - do not tunnel to it."
      Start-Sleep -Seconds 30
      continue
    }
    Write-Host "[c4] starting host solver..."
    # QUOTE THE PATH. Start-Process joins -ArgumentList with spaces and hands the result to the
    # exe as one command line, so an unquoted path containing a space is split by the callee.
    # MEASURED: this repo lives at "...\Production crawler\...", and python reported
    #   can't open file 'C:\Users\...\Desktop\mallery\Production': No such file or directory
    # The solver therefore never started, the supervisor's own log said only "starting host
    # solver...", and C4 sat down. It went unnoticed because the dev checkout is "cralwer" with no
    # space in it — the bug only exists on the production mirror, which is the copy that matters.
    Start-Process -FilePath $python -ArgumentList @("`"$solver`"") -WindowStyle Minimized

    # POLL, do not guess. This waited a flat 6s, but the solver imports seleniumbase before it
    # binds and that routinely takes longer on a cold filesystem cache. So Test-Solver said "no",
    # the loop hit `continue`, and the next pass STARTED ANOTHER ONE - while the first was still
    # coming up. That is where the two host_solver processes came from, and two of them fighting
    # over one Chrome is the exact thing this script's own port check exists to prevent.
    #
    # Polling also means a fast start is detected in 3s instead of always costing 6.
    $ok = $false
    for ($w = 0; $w -lt 20; $w++) {          # up to ~60s
      Start-Sleep -Seconds 3
      if (Test-Solver) { $ok = $true; break }
    }
    if ($ok) {
      Write-Host "[c4] solver up on 127.0.0.1:$SolverPort (after $(($w + 1) * 3)s)"
    } else {
      Write-Host "[c4] solver did not come up within 60s - not starting a second one; retrying"
      Start-Sleep -Seconds 15
      continue
    }
  }

  # --- 2. the tunnel -----------------------------------------------------------------------
  # ssh holds the link open; when it returns, the link died and the loop rebuilds it.
  #
  #   ServerAliveInterval/CountMax : notice a dead peer in ~45s instead of hanging half-open
  #   ExitOnForwardFailure         : if the remote bind fails, FAIL rather than sit connected with
  #                                  no forward - a tunnel that is up but forwarding nothing is
  #                                  the silent state this whole exercise is about
  #   -N                           : no shell, just the forward
  #
  # IT MUST NOT BLOCK HERE, AND THAT WAS THE BUG. `& ssh` ran in the foreground, so this loop sat
  # inside it for the WHOLE life of a healthy tunnel and the solver check at the top of the loop
  # was unreachable the entire time. A supervisor that only supervises the tunnel is half a
  # supervisor: kill the solver and ssh happily forwards to a closed port for hours. MEASURED
  # 2026-08-16 - the solver was stopped, ssh stayed connected, and C4 was down with this script
  # running and reporting nothing. That is the same shape as the stale remote listener above:
  # a healthy-looking pipe to a service that is not there.
  #
  # So ssh runs as a child and BOTH ends are polled. Either one dying takes the other down, and the
  # loop rebuilds the pair - which also keeps the solver and the tunnel from drifting out of step.
  Write-Host "[c4] $(Get-Date -Format 'HH:mm:ss') connecting tunnel (remote :$TunnelPort)..."
  $t0 = Get-Date
  $sshArgs = @("-N", "-i", "`"$KeyPath`"", "-p", "$RemotePort",
               "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3",
               "-o", "StrictHostKeyChecking=accept-new",
               "-R", "${TunnelPort}:127.0.0.1:${SolverPort}",
               "$RemoteUser@$RemoteHost")
  $ssh = Start-Process -FilePath "ssh" -ArgumentList $sshArgs -PassThru -NoNewWindow
  $solverDied = $false
  while (-not $ssh.HasExited) {
    Start-Sleep -Seconds 15
    if (-not (Test-Solver)) {
      # Confirm before acting: one failed probe during a ~30s cold Chrome launch is not a death,
      # and tearing the tunnel down on a false positive is its own outage.
      Start-Sleep -Seconds 10
      if (-not (Test-Solver)) {
        Write-Host "[c4] $(Get-Date -Format 'HH:mm:ss') SOLVER GONE while the tunnel was healthy - taking the tunnel down and restarting both"
        $solverDied = $true
        Stop-Process -Id $ssh.Id -Force -ErrorAction SilentlyContinue
        break
      }
    }
  }
  $ssh.WaitForExit()
  $LASTEXITCODE = $ssh.ExitCode
  $held = ((Get-Date) - $t0).TotalSeconds
  if ($solverDied) {
    Start-Sleep -Seconds 2
    continue
  }

  # A connection that dies in under 5s did not "drop" - it was REFUSED, and by far the most likely
  # reason is a stale remote listener from a previous session. Retrying at 10s intervals against
  # that is an infinite loop that keeps C4 down while looking busy, so name it and back off instead
  # of hammering. Backing off does not fix it; only moving $TunnelPort (or root reaping the old
  # sshd) does. Say so, because a log line that describes the wrong problem costs more than silence.
  if ($held -lt 5) {
    Write-Host "[c4] $(Get-Date -Format 'HH:mm:ss') tunnel REFUSED after $([math]::Round($held,1))s (exit $LASTEXITCODE)"
    Write-Host "[c4]   If this says 'remote port forwarding failed', :$TunnelPort is still held on"
    Write-Host "[c4]   the remote by a dead session. That listener ACCEPTS TCP and never answers,"
    Write-Host "[c4]   so only a content check sees it. Reaping needs root there; otherwise restart"
    Write-Host "[c4]   this script with -TunnelPort <free port> and match C4_TUNNEL_PORT in .env."
    Start-Sleep -Seconds 60
  } else {
    Write-Host "[c4] $(Get-Date -Format 'HH:mm:ss') tunnel closed after $([math]::Round($held,0))s (exit $LASTEXITCODE) - reconnecting in 10s"
    Start-Sleep -Seconds 10
  }
}
