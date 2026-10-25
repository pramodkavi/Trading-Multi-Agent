import { safeHttpsUrl } from "./links";

describe("safeHttpsUrl", () => {
  it("returns an https URL unchanged", () => {
    expect(safeHttpsUrl("https://github.com/o/r/pull/30")).toBe("https://github.com/o/r/pull/30");
  });

  it("rejects every other scheme and malformed input", () => {
    for (const bad of [
      "javascript:alert(1)",
      "http://github.com/o/r",
      "data:text/html,<script>1</script>",
      "//github.com/o/r",
      "not a url",
      "",
      null,
      undefined,
    ]) {
      expect(safeHttpsUrl(bad)).toBeNull();
    }
  });
});
