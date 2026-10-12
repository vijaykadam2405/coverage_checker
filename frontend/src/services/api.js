export async function api(path, body, method = "POST", { signal } = {}) {
  const response = await fetch(
    `/api${path}`,
    body === undefined
      ? { cache: "no-store", signal }
      : {
          method,
          signal,
          headers: {
            "Content-Type": "application/json",
            "X-CrawlScope-Client": "1",
          },
          body: JSON.stringify(body),
        },
  );
  const data = await response.json();
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Please check the website URL and select at least one capture type.",
    );
  return data;
}
