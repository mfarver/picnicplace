import { describe, expect, it } from "vitest";
import app from "./app.css?raw";
import tokens from "./tokens.css?raw";

/** Every rule nested inside another rule (not an at-rule, nor an `&` rule), as
 * "line: selector". Browsers accept CSS nesting, so a rule left without its closing brace,
 * as a bad merge can leave one, swallows the rules after it with no error anywhere. */
function nestedRules(css: string): string[] {
  const text = css.replace(/\/\*[\s\S]*?\*\//g, (c) => c.replace(/[^\n]/g, " "));
  const stack: string[] = [];
  const found: string[] = [];
  let line = 1;
  let buf = "";
  for (const ch of text) {
    if (ch === "\n") line += 1;
    if (ch === "{") {
      const selector = (buf.split(";").pop() ?? "").trim();
      const parent = stack[stack.length - 1];
      if (parent !== undefined && !parent.startsWith("@") && !selector.startsWith("&")) {
        found.push(`${line}: ${selector}`);
      }
      stack.push(selector);
      buf = "";
    } else if (ch === "}") {
      stack.pop();
      buf = "";
    } else {
      buf += ch;
    }
  }
  if (stack.length > 0) found.push(`unclosed: ${stack.join(" > ")}`);
  return found;
}

describe("stylesheets", () => {
  it("close every rule, so none is nested in another by mistake", () => {
    expect(nestedRules(app)).toEqual([]);
    expect(nestedRules(tokens)).toEqual([]);
  });

  it("catch a rule left open", () => {
    expect(nestedRules(".a {\n  color: red;\n.b {\n  color: blue;\n}\n}\n")).toEqual([
      "3: .b",
    ]);
  });
});
