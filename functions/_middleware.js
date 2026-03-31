const AUTH_USER = "bukken";
const AUTH_PASS = "shibuya2026dt";

function unauthorized() {
  return new Response("Unauthorized", {
    status: 401,
    headers: { "WWW-Authenticate": 'Basic realm="bukken-dashboard"' },
  });
}

export async function onRequest(context) {
  const auth = context.request.headers.get("Authorization");
  if (!auth || !auth.startsWith("Basic ")) {
    return unauthorized();
  }

  const decoded = atob(auth.slice(6));
  const [user, pass] = decoded.split(":");

  if (user !== AUTH_USER || pass !== AUTH_PASS) {
    return unauthorized();
  }

  return context.next();
}
