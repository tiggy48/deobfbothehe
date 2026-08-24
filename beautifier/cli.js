const fs = require("fs");
const beautify = require("./lua_beautifier.js");

const inPath = process.argv[2];
const outPath = process.argv[3];

const src = fs.readFileSync(inPath, "utf-8");
const result = beautify(src);
fs.writeFileSync(outPath, result, "utf-8");
