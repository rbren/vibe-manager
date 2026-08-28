#!/usr/bin/env node

import { readFile, realpath, stat } from "node:fs/promises";
import path from "node:path";
import process from "node:process";

const NAME_PATTERN = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;
const VERSION_PATTERN = /^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$/;
const IMPORT_PATTERN = /(?:^|[;\n])\s*import\s+(?:[^"'()]*?\s+from\s+)?["']([^"']+)["']|(?:^|[;\n])\s*export\s+[^"']*?\s+from\s+["']([^"']+)["']/gm;
const DYNAMIC_IMPORT_PATTERN = /\bimport\s*\(/;
const REMOTE_IMPORT_PATTERN = /\b(?:import|export)\s+(?:[^"']*?\s+from\s+)?["'](?:https?:)?\/\//m;
const NODE_RUNTIME_PATTERN = /\b(?:require\s*\(|module\.exports|process\.env|__dirname|__filename|node:[a-z])/;
const ACTIVATE_EXPORT_PATTERN = /\bexport\s+(?:async\s+)?function\s+activate\b|\bexport\s*\{[^}]*\bactivate\b[^}]*\}/m;
const REGISTER_PAGE_PATTERN = /\.registerPage\s*\(\s*["']([^"']+)["']/g;

const errors = [];
const warnings = [];

function fail(message) {
  errors.push(message);
}

function warn(message) {
  warnings.push(message);
}

function requireString(record, field) {
  const value = record[field];
  if (typeof value !== "string" || value.trim() === "") {
    fail(`Manifest field "${field}" must be a non-empty string.`);
    return null;
  }
  return value;
}

function isInside(root, candidate) {
  const relative = path.relative(root, candidate);
  return relative !== "" && !relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative);
}

function validatePage(page, index, ids, paths) {
  const prefix = `contributes.pages[${index}]`;
  if (!page || typeof page !== "object" || Array.isArray(page)) {
    fail(`${prefix} must be an object.`);
    return;
  }

  const id = requireString(page, "id");
  const title = requireString(page, "title");
  const route = requireString(page, "path");
  void title;

  if (id && !NAME_PATTERN.test(id)) {
    fail(`${prefix}.id must use lowercase kebab-case.`);
  }
  if (id && ids.has(id)) fail(`Page id "${id}" is declared more than once.`);
  if (id) ids.add(id);

  if (route) {
    const segments = route.slice(1).split("/");
    if (!route.startsWith("/") || route.startsWith("//") || segments.some((segment) => !NAME_PATTERN.test(segment))) {
      fail(`${prefix}.path must be an absolute route of kebab-case segments.`);
    }
    if (paths.has(route)) fail(`Page path "${route}" is declared more than once.`);
    paths.add(route);
  }

  for (const optional of ["nav_label", "description"]) {
    if (page[optional] !== undefined && page[optional] !== null && typeof page[optional] !== "string") {
      fail(`${prefix}.${optional} must be a string or null when present.`);
    }
  }
}

async function main() {
  const input = process.argv[2];
  if (!input || input === "--help" || input === "-h") {
    console.log("Usage: validate-extension.mjs <extension-directory>");
    process.exit(input ? 0 : 2);
  }

  const root = path.resolve(input);
  try {
    if (!(await stat(root)).isDirectory()) throw new Error("not a directory");
  } catch {
    console.error(`ERROR: Extension directory does not exist: ${root}`);
    process.exit(1);
  }

  const manifestPath = path.join(root, "canvas-extension.json");
  let manifest;
  try {
    manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  } catch (error) {
    console.error(`ERROR: Cannot read valid JSON from ${manifestPath}: ${error.message}`);
    process.exit(1);
  }

  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) {
    fail("Manifest root must be an object.");
  } else {
    if (manifest.schema_version !== 1) {
      fail('Manifest field "schema_version" must equal 1.');
    }

    const name = requireString(manifest, "name");
    const version = requireString(manifest, "version");
    requireString(manifest, "entrypoint");

    if (name && !NAME_PATTERN.test(name)) {
      fail('Manifest field "name" must use lowercase kebab-case.');
    }
    if (version && !VERSION_PATTERN.test(version)) {
      fail('Manifest field "version" should be a semantic version such as "0.1.0".');
    }
    for (const optional of ["display_name", "description"]) {
      if (manifest[optional] !== undefined && manifest[optional] !== null && typeof manifest[optional] !== "string") {
        fail(`Manifest field "${optional}" must be a string or null when present.`);
      }
    }
  }

  const ids = new Set();
  const paths = new Set();
  const contributes = manifest?.contributes;
  if (contributes !== undefined && contributes !== null && (typeof contributes !== "object" || Array.isArray(contributes))) {
    fail('Manifest field "contributes" must be an object or null when present.');
  }
  const pages = contributes?.pages;
  if (pages !== undefined && pages !== null && !Array.isArray(pages)) {
    fail('Manifest field "contributes.pages" must be an array or null when present.');
  }
  if (Array.isArray(pages)) {
    pages.forEach((page, index) => validatePage(page, index, ids, paths));
  }
  if (!Array.isArray(pages) || pages.length === 0) {
    warn("The manifest declares no page contributions; routed pages are the only proven v1 surface.");
  }

  let source = null;
  const entrypoint = manifest?.entrypoint;
  if (typeof entrypoint === "string" && entrypoint.trim()) {
    const resolvedEntrypoint = path.resolve(root, entrypoint);
    if (!isInside(root, resolvedEntrypoint)) {
      fail("Manifest entrypoint must resolve to a file inside the extension root.");
    } else {
      try {
        const [realRoot, realEntrypoint] = await Promise.all([
          realpath(root),
          realpath(resolvedEntrypoint),
        ]);
        if (!isInside(realRoot, realEntrypoint)) {
          fail("Manifest entrypoint escapes the extension root through a symbolic link.");
        } else if (!(await stat(realEntrypoint)).isFile()) {
          fail(`Manifest entrypoint is not a file: ${entrypoint}`);
        } else {
          source = await readFile(realEntrypoint, "utf8");
        }
      } catch (error) {
        fail(`Cannot read manifest entrypoint "${entrypoint}": ${error.message}`);
      }
    }
  }

  if (source !== null) {
    if (!ACTIVATE_EXPORT_PATTERN.test(source)) {
      fail('Entrypoint must export an "activate" function.');
    }

    for (const match of source.matchAll(IMPORT_PATTERN)) {
      const specifier = match[1] ?? match[2];
      if (specifier && !specifier.startsWith(".") && !specifier.startsWith("/") && !specifier.startsWith("data:") && !specifier.startsWith("blob:")) {
        fail(`Entrypoint contains an unresolved bare import: ${specifier}`);
      } else if (specifier?.startsWith(".")) {
        fail(`Entrypoint contains a relative import that cannot resolve from its Blob URL: ${specifier}`);
      }
    }
    if (REMOTE_IMPORT_PATTERN.test(source)) fail("Entrypoint contains a remote module import.");
    if (DYNAMIC_IMPORT_PATTERN.test(source)) warn("Entrypoint contains dynamic import(); confirm it does not load another chunk.");
    if (NODE_RUNTIME_PATTERN.test(source)) warn("Entrypoint may depend on Node-specific runtime globals or modules.");
    if (/sourceMappingURL=.*\.js\.map/m.test(source)) warn("Entrypoint references an external source map; confirm no runtime asset is required.");

    const registeredIds = new Set();
    for (const match of source.matchAll(REGISTER_PAGE_PATTERN)) registeredIds.add(match[1]);
    for (const registeredId of registeredIds) {
      if (!ids.has(registeredId)) fail(`Entrypoint registers undeclared page id "${registeredId}".`);
    }
    for (const declaredId of ids) {
      if (!registeredIds.has(declaredId)) warn(`Declared page id "${declaredId}" was not found in a static registerPage call.`);
    }
  }

  for (const message of warnings) console.log(`WARNING: ${message}`);
  for (const message of errors) console.error(`ERROR: ${message}`);

  if (errors.length) {
    console.error(`\nValidation failed with ${errors.length} error(s) and ${warnings.length} warning(s).`);
    process.exit(1);
  }
  console.log(`\nValidation passed with ${warnings.length} warning(s): ${root}`);
}

await main();
