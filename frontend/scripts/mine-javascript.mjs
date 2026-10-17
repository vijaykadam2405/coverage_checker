// Static analysis only. Input source is parsed, never evaluated or imported.
import { parse } from "acorn";
import { createInterface } from "node:readline";

const interface_ = createInterface({
  input: process.stdin,
  crlfDelay: Infinity,
});
for await (const line of interface_) {
  try {
    const { source } = JSON.parse(line);
    let tree;
    try {
      tree = parse(source, { ecmaVersion: "latest", sourceType: "module" });
    } catch {
      tree = parse(source, { ecmaVersion: "latest", sourceType: "script" });
    }
    const urls = new Set();
    const patterns = new Set();
    const imports = new Set();
    const stack = [tree];
    while (stack.length) {
      const node = stack.pop();
      if (!node || typeof node !== "object") continue;
      if (
        [
          "ImportDeclaration",
          "ImportExpression",
          "ExportNamedDeclaration",
          "ExportAllDeclaration",
        ].includes(node.type)
      ) {
        const source = node.source?.value;
        if (
          typeof source === "string" &&
          /^(?:\.{1,2}\/|\/|https?:\/\/)/.test(source)
        )
          imports.add(source);
      }
      const value =
        node.type === "Literal"
          ? node.value
          : node.type === "TemplateLiteral" && node.expressions.length === 0
            ? node.quasis.map((item) => item.value.cooked).join("")
            : null;
      if (
        typeof value === "string" &&
        /^(?:https?:\/\/|\/(?!\/))/.test(value) &&
        !/[\s<>]/.test(value)
      ) {
        if (/[{*]|\/:\w|\[\w+\]/.test(value)) patterns.add(value);
        else urls.add(value);
      }
      for (const item of Object.values(node)) {
        if (Array.isArray(item)) {
          for (const child of item) stack.push(child);
        } else if (item && typeof item === "object") stack.push(item);
      }
    }
    process.stdout.write(
      JSON.stringify({
        urls: [...urls],
        patterns: [...patterns],
        imports: [...imports],
      }) + "\n",
    );
  } catch (error) {
    process.stdout.write(
      JSON.stringify({ error: String(error.message), urls: [], patterns: [] }) +
        "\n",
    );
  }
}
