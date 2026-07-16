export default {
  async email(message, env, ctx) {
    const raw = await new Response(message.raw).arrayBuffer();
    const rawBase64 = arrayBufferToBase64(raw);
    const payload = JSON.stringify({
      from: message.from,
      to: message.to,
      raw_base64: rawBase64,
      mailbox: "INBOX",
    });

    const timestamp = Math.floor(Date.now() / 1000).toString();
    const signature = await sign(env.INGEST_SECRET, `${timestamp}.${payload}`);

    const response = await fetch(env.INGEST_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-pickup-timestamp": timestamp,
        "x-pickup-signature": signature,
      },
      body: payload,
    });

    if (!response.ok) {
      message.setReject(`pickup ingest failed: ${response.status}`);
    }
  },
};

function arrayBufferToBase64(buffer) {
  let binary = "";
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunkSize));
  }
  return btoa(binary);
}

async function sign(secret, value) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value));
  return [...new Uint8Array(signature)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}
