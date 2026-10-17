import { api, ApiError, FileRef, fileSize, RunLimits } from "./api";

export const DEFAULT_RUN_FILE_BYTES = 2 * 1024 ** 3;
export const DEFAULT_RAW_FILE_BYTES = 25 * 1024 ** 2;
export const DEFAULT_FILE_COUNT = 20;
const CHUNK_BYTES = 8 * 1024 ** 2;
const CHUNK_ALIGNMENT = 256 * 1024;
const ATTEMPTS = 5;

export type UploadStatus = "starting" | "uploading" | "retrying" | "finalizing";
export interface UploadUpdate {
  progress: number;
  status: UploadStatus;
}
interface UploadSession {
  id: string;
  upload_url: string;
  chunk_size: number;
  headers?: Record<string, string>;
}
interface ChunkResponse {
  status: number;
  range: string | null;
  body: string;
}
class UploadHttpError extends Error {
  constructor(public status: number) {
    super(
      status === 404 || status === 410
        ? "The upload session expired. Retry to start a new upload."
        : status === 401 || status === 403
          ? "Upload access expired. Refresh your session and retry."
          : `Upload interrupted (${status}). Retry the file.`,
    );
  }
}
const aborted = () => new DOMException("Upload cancelled", "AbortError");
function checkAbort(signal: AbortSignal) {
  if (signal.aborted) throw aborted();
}
function retryable(error: unknown) {
  if (error instanceof Error && error.name === "AbortError") return false;
  if (error instanceof ApiError || error instanceof UploadHttpError)
    return error.status === 408 || error.status === 429 || error.status >= 500;
  return true;
}
function backoff(attempt: number, signal: AbortSignal): Promise<void> {
  checkAbort(signal);
  return new Promise((resolve, reject) => {
    const cancel = () => {
      clearTimeout(timer);
      reject(aborted());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", cancel);
      resolve();
    }, Math.min(16_000, 1000 * 2 ** attempt));
    signal.addEventListener("abort", cancel, { once: true });
  });
}

/** Validate every selected file, including pending and failed attachments. */
export function validateAttachment(
  file: Pick<File, "name" | "size">,
  currentBytes: number,
  currentCount: number,
  limits: RunLimits,
): string | null {
  const count = limits.max_files_per_run ?? DEFAULT_FILE_COUNT;
  const total = limits.max_run_bytes ?? DEFAULT_RUN_FILE_BYTES;
  const perFile = limits.resumable_uploads
    ? (limits.max_file_bytes ?? DEFAULT_RUN_FILE_BYTES)
    : Math.min(
        limits.max_file_bytes ?? DEFAULT_RUN_FILE_BYTES,
        limits.raw_upload_max_bytes ?? DEFAULT_RAW_FILE_BYTES,
      );
  if (currentCount >= count) return `You can attach up to ${count} files per session.`;
  if (!file.size) return `${file.name} is empty. Choose a file with content.`;
  if (file.size > perFile)
    return `${file.name} exceeds the current ${fileSize(perFile)} per-file limit.`;
  if (currentBytes + file.size > total)
    return `These files exceed ${fileSize(total)} in total. Remove a file or choose a smaller one.`;
  return null;
}

/** Parse the committed byte count, never an arbitrary Content-Range expression. */
export function committedBytes(range: string | null, total: number): number {
  if (!range) return 0;
  const match = /^bytes=0-(\d+)$/i.exec(range.trim());
  const bytes = match ? Number(match[1]) + 1 : NaN;
  if (!Number.isSafeInteger(bytes) || bytes < 1 || bytes > total)
    throw new Error("The upload returned an invalid progress acknowledgement. Retry the file.");
  return bytes;
}

function transfer(
  url: string,
  body: Blob,
  headers: Record<string, string>,
  signal: AbortSignal,
  progress: (loaded: number) => void,
  method: "POST" | "PUT" = "PUT",
): Promise<ChunkResponse> {
  checkAbort(signal);
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const cancel = () => xhr.abort();
    const finish = (error?: Error, response?: ChunkResponse) => {
      signal.removeEventListener("abort", cancel);
      if (error) reject(error);
      else resolve(response!);
    };
    xhr.open(method, url);
    // The cloud session URL grants this upload only. Never send owner cookies there.
    xhr.withCredentials = method === "POST";
    xhr.timeout = 5 * 60 * 1000;
    for (const [name, value] of Object.entries(headers)) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => progress(event.loaded);
    xhr.onload = () => {
      if ((xhr.status >= 200 && xhr.status < 300) || xhr.status === 308)
        finish(undefined, {
          status: xhr.status,
          range: xhr.getResponseHeader("Range"),
          body: xhr.responseText,
        });
      else finish(new UploadHttpError(xhr.status));
    };
    xhr.onerror = () => finish(new Error("The connection was interrupted. Check your connection and retry."));
    xhr.ontimeout = () => finish(new Error("The connection timed out. Check your connection and retry."));
    xhr.onabort = () => finish(aborted());
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) {
      signal.removeEventListener("abort", cancel);
      reject(aborted());
      return;
    }
    xhr.send(body);
  });
}

/** Upload bounded Blob slices. Session credentials live in this call, never storage. */
export async function uploadFile(
  file: File,
  limits: RunLimits,
  signal: AbortSignal,
  update: (state: UploadUpdate) => void,
): Promise<FileRef> {
  checkAbort(signal);
  update({ status: "starting", progress: 0 });
  let stagedId: string | undefined;
  try {
    if (!limits.resumable_uploads) {
      if (file.size > (limits.raw_upload_max_bytes ?? DEFAULT_RAW_FILE_BYTES))
        throw new Error("Large uploads are unavailable. Refresh and check Connections.");
      const response = await transfer(
        "/api/files", file,
        { "X-File-Name": encodeURIComponent(file.name), "Content-Type": file.type || "application/octet-stream" },
        signal,
        (loaded) => update({ status: loaded >= file.size ? "finalizing" : "uploading", progress: Math.min(99, Math.floor(loaded / file.size * 100)) }),
        "POST",
      );
      const ref = JSON.parse(response.body) as FileRef;
      stagedId = ref.id;
      checkAbort(signal);
      return ref;
    }

    // Do not abort initiation before its id arrives: cancellation must release its reservation.
    const session = await api<UploadSession>("/api/files/uploads", {
      method: "POST",
      signal: AbortSignal.timeout(60_000),
      body: JSON.stringify({ name: file.name, mime: file.type || "application/octet-stream", size: file.size }),
    });
    stagedId = session.id;
    checkAbort(signal);
    const parsed = new URL(session.upload_url);
    if (parsed.protocol !== "https:" || (parsed.hostname !== "storage.googleapis.com" && !parsed.hostname.endsWith(".storage.googleapis.com")))
      throw new Error("The upload service returned an unsupported destination.");
    const requestedChunk = Math.min(CHUNK_BYTES, session.chunk_size || CHUNK_BYTES);
    const chunkSize = Math.max(CHUNK_ALIGNMENT, Math.floor(requestedChunk / CHUNK_ALIGNMENT) * CHUNK_ALIGNMENT);
    const contentType = session.headers?.["Content-Type"] || file.type || "application/octet-stream";
    let offset = 0;
    let failures = 0;
    let uncertain = false;

    while (offset < file.size) {
      checkAbort(signal);
      try {
        if (uncertain) {
          const status = await transfer(
            session.upload_url, new Blob(),
            { "Content-Type": contentType, "Content-Range": `bytes */${file.size}` },
            signal, () => {},
          );
          offset = status.status === 308 ? committedBytes(status.range, file.size) : file.size;
          uncertain = false;
          if (offset === file.size) break;
        }
        const end = Math.min(offset + chunkSize, file.size);
        const start = offset;
        const response = await transfer(
          session.upload_url, file.slice(start, end),
          { "Content-Type": contentType, "Content-Range": `bytes ${start}-${end - 1}/${file.size}` },
          signal,
          (loaded) => update({ status: "uploading", progress: Math.min(99, Math.floor((start + loaded) / file.size * 100)) }),
        );
        const confirmed = response.status === 308 ? committedBytes(response.range, file.size) : file.size;
        if (confirmed <= start || confirmed > end)
          throw new Error("Upload progress could not be confirmed. Retrying the connection.");
        offset = confirmed;
        failures = 0;
        update({ status: "uploading", progress: Math.min(99, Math.floor(offset / file.size * 100)) });
      } catch (error) {
        if (!retryable(error) || ++failures >= ATTEMPTS) throw error;
        uncertain = true;
        update({ status: "retrying", progress: Math.min(99, Math.floor(offset / file.size * 100)) });
        await backoff(failures - 1, signal);
      }
    }
    update({ status: "finalizing", progress: 99 });
    for (let attempt = 0; ; attempt++) {
      checkAbort(signal);
      try {
        const ref = await api<FileRef>(`/api/files/uploads/${encodeURIComponent(session.id)}/complete`, { method: "POST", signal: AbortSignal.timeout(90_000) });
        checkAbort(signal);
        return ref;
      } catch (error) {
        if (!retryable(error) || attempt + 1 >= ATTEMPTS) throw error;
        await backoff(attempt, signal);
      }
    }
  } catch (error) {
    if (stagedId) await api(`/api/files/${encodeURIComponent(stagedId)}`, { method: "DELETE", signal: AbortSignal.timeout(10_000) }).catch(() => {});
    throw error;
  }
}
