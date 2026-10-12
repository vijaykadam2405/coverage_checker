/** Encode server filters; never filter or export an in-memory inventory. */
export function inventoryQuery(filters = {}, page = null) {
  const values = Object.fromEntries(
    Object.entries(filters).filter(
      ([, value]) => value !== undefined && value !== null && value !== "",
    ),
  );
  if (page !== null) Object.assign(values, { page, page_size: 50 });
  return new URLSearchParams(values).toString();
}
