import { FileRef, fileSize } from "@/lib/api";
import { isZipArchive, ZIP_COVERAGE_NOTE } from "@/lib/file-coverage";

export default function FileCoverage({ files }: { files: FileRef[] }) {
  if (!files.length) return null;
  const incomplete = files.some((file) => file.preview_complete === false);
  return (
    <details className={"file-coverage " + (incomplete ? "has-partial" : "")}>
      <summary>File coverage <span>{incomplete ? "Limited previews · check coverage" : `${files.length} stored ${files.length === 1 ? "file" : "files"}`}</span></summary>
      <p>Uploading a file stores the original. It does not mean the models inspected every byte. The preview coverage below is separate from any later analysis or validation.</p>
      <ul>{files.map((file) => <li key={file.id}>
        <div><strong>{file.name}</strong><span>{fileSize(file.size)} original{file.storage ? ` · ${file.storage === "cloud" ? "cloud" : "local"} storage` : ""}</span></div>
        <p>{file.coverage_note || (typeof file.preview_complete === "boolean" ? file.preview_complete ? "The full file was available to the preview extractor; format and sampling limits may still apply. Models receive selected content." : "A bounded preview is available; complete-file analysis has not been established." : "Preview coverage was not recorded for this file.")}</p>
        {isZipArchive(file.name, file.mime) && <p>{ZIP_COVERAGE_NOTE}</p>}
        {typeof file.preview_bytes === "number" && <small>{fileSize(file.preview_bytes)} processed for preview / {fileSize(file.size)} stored</small>}
      </li>)}</ul>
    </details>
  );
}
