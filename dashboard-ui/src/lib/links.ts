/** The URL when it is a well-formed `https:` URL, else `null` (never `javascript:`, `data:`, `http:`). */
export function safeHttpsUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    return new URL(value).protocol === "https:" ? value : null;
  } catch {
    return null;
  }
}
