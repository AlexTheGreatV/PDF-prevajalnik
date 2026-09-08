import { copyFile, mkdir, readFile, stat } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const appRoot = path.resolve(__dirname, "..");

const expectedVersions = {
  "tesseract.js": "6.0.1",
  "tesseract.js-core": "6.1.2",
  "@tesseract.js-data/eng": "1.0.0",
};

const files = [
  {
    packageName: "tesseract.js",
    source: "node_modules/tesseract.js/dist/worker.min.js",
    target: "public/vendor/tesseract/worker.min.js",
  },
  {
    packageName: "tesseract.js-core",
    source:
      "node_modules/.pnpm/tesseract.js-core@6.1.2/node_modules/tesseract.js-core/tesseract-core.wasm.js",
    target: "public/vendor/tesseract/tesseract-core.wasm.js",
  },
  {
    packageName: "tesseract.js-core",
    source:
      "node_modules/.pnpm/tesseract.js-core@6.1.2/node_modules/tesseract.js-core/tesseract-core.wasm",
    target: "public/vendor/tesseract/tesseract-core.wasm",
  },
  {
    packageName: "@tesseract.js-data/eng",
    source: "node_modules/@tesseract.js-data/eng/4.0.0/eng.traineddata.gz",
    target: "public/vendor/tesseract/lang/eng.traineddata.gz",
  },
];

async function readPackageJson(packageName) {
  const packageJsonPath =
    packageName === "tesseract.js-core"
      ? path.join(appRoot, "node_modules/.pnpm/tesseract.js-core@6.1.2/node_modules/tesseract.js-core/package.json")
      : path.join(appRoot, "node_modules", packageName, "package.json");

  try {
    return JSON.parse(await readFile(packageJsonPath, "utf8"));
  } catch (error) {
    throw new Error(
      `OCR runtime package ${packageName} is missing. Run pnpm install before preparing OCR assets. (${error.message})`,
    );
  }
}

for (const [packageName, expectedVersion] of Object.entries(expectedVersions)) {
  const packageJson = await readPackageJson(packageName);
  if (packageJson.version !== expectedVersion) {
    throw new Error(
      `OCR runtime package ${packageName} has version ${packageJson.version}, expected ${expectedVersion}. Check apps/web/pnpm-lock.yaml.`,
    );
  }
}

for (const file of files) {
  const source = path.join(appRoot, file.source);
  const target = path.join(appRoot, file.target);

  try {
    await stat(source);
  } catch (error) {
    throw new Error(
      `OCR runtime source is missing for ${file.packageName}: ${file.source}. Run pnpm install and keep the lockfile versions unchanged. (${error.message})`,
    );
  }

  await mkdir(path.dirname(target), { recursive: true });
  await copyFile(source, target);
  console.log(`prepared ${file.target}`);
}
