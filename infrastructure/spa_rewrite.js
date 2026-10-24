// CloudFront Function (cloudfront-js-2.0), viewer-request, DEFAULT (S3) behaviour only.
//
// SPA fallback (Slice 4 Step 4.5, design §8): any request whose LAST path segment
// has no "." is a client-side route ("/", "/callback", "/signals/abc") and is
// served /index.html; files ("/assets/x.js", "/config.json", "/favicon.svg") pass
// through untouched. Done here instead of distribution-level custom error
// responses, which would also rewrite the API's real 403/404 into index.html.
// The /api/* behaviour has NO function association.
function handler(event) {
  var request = event.request;
  var uri = request.uri;
  var lastSegment = uri.substring(uri.lastIndexOf("/") + 1);
  if (lastSegment.indexOf(".") === -1) {
    request.uri = "/index.html";
  }
  return request;
}
