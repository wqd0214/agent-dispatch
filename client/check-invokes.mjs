// 核对本地页面里 invoke 的命令名和参数名是否与 Rust 侧注册的命令一致（Tauri 把参数的小驼峰映射到蛇形）
import { readFileSync } from "node:fs";
const rust = readFileSync(new URL("./src-tauri/src/lib.rs", import.meta.url), "utf8");
const registered = rust.match(/generate_handler!\[([^\]]*)\]/)[1].split(",").map(s => s.trim()).filter(Boolean);
const params = {};
for (const name of registered) {
  const m = rust.match(new RegExp(`fn ${name}\\s*\\(([^)]*)\\)`));
  if (!m) throw new Error(`找不到命令函数 ${name}`);
  params[name] = m[1].split(",").map(a => a.trim()).filter(Boolean)
    .filter(a => !/:\s*(AppHandle|State|tauri::)/.test(a) && !/^(app|state)\s*:/.test(a))
    .map(a => a.split(":")[0].trim().replace(/^mut\s+/, ""));
}
const camel = s => s.replace(/_([a-z])/g, (_, c) => c.toUpperCase());
let failed = 0;
for (const file of ["src/index.html", "src/settings.html"]) {
  const html = readFileSync(new URL(`./${file}`, import.meta.url), "utf8");
  for (const m of html.matchAll(/invoke\("([a-z_]+)"(?:\s*,\s*\{([^}]*)\})?/g)) {
    const [, name, body] = m;
    if (!registered.includes(name)) { console.error(`✗ ${file}: 命令 ${name} 未注册（已注册：${registered.join("、")}）`); failed++; continue; }
    const given = (body || "").split(",").map(p => p.split(":")[0].trim()).filter(Boolean);
    const expected = params[name].map(camel);
    const missing = expected.filter(p => !given.includes(p) && !(given.length === 1 && given[0] === p));
    const extra = given.filter(p => !expected.includes(p));
    if (missing.length || extra.length) { console.error(`✗ ${file}: ${name} 参数不符，缺 [${missing}] 多 [${extra}]，应为 [${expected}]`); failed++; }
    else console.log(`✓ ${file}: ${name}(${given.join(", ")})`);
  }
}
process.exit(failed ? 1 : 0);
