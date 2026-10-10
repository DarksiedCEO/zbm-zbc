// Bug sweep E, F-5: prints a DASHBOARD_PASSWORD_HASH value (scrypt, N=2^15,
// r=8, p=1, 16-byte salt, 32-byte key) for the password read from stdin — never
// from argv, so it stays out of shell history and the process list.
//
//   npm run hash-password            (type the password, then Enter / Ctrl-D)
//   printf '%s' "$PW" | npm run --silent hash-password
import { randomBytes, scrypt } from "node:crypto";

const N = 32768, r = 8, p = 1;
const chunks = [];
for await (const c of process.stdin) chunks.push(c);
const password = Buffer.concat(chunks).toString("utf8").replace(/\r?\n$/, "");
if (!password || Buffer.byteLength(password) > 1024) {
  console.error("hash-password: the password must be 1..1024 bytes (read from stdin)");
  process.exit(2);
}
if (password.length < 12) console.error("hash-password: WARNING: use at least 12 characters");
const salt = randomBytes(16);
scrypt(Buffer.from(password, "utf8"), salt, 32, { N, r, p, maxmem: 256 * N * r + 1024 * 1024 }, (err, key) => {
  if (err) {
    console.error(`hash-password: ${err.message}`);
    process.exit(1);
  }
  console.log(`scrypt$${N}$${r}$${p}$${salt.toString("base64url")}$${key.toString("base64url")}`);
});
