import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const eslintConfig = [
  {
    ignores: ["public/vendor/tesseract/**"],
  },
  ...nextVitals,
  ...nextTypescript,
];

export default eslintConfig;
