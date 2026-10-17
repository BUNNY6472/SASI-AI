export const ZIP_COVERAGE_NOTE = "Large ZIP archives are stored unchanged; their member files are not automatically inspected. Upload relevant extracted files for analysis, and check the reported preview coverage.";

export function isZipArchive(name: string, mime?: string): boolean {
  return /\.zip$/i.test(name) || mime === "application/zip" || mime === "application/x-zip-compressed";
}
