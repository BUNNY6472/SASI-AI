/** New questions use the bridge; legacy timing/approval fields are never forwarded. */
export function questionRequest(input: {
  request: string;
  materials: string;
  eris: boolean;
  mode: "quick" | "adaptive" | "deep";
  file_ids: string[];
}) {
  return {
    request: input.request.trim(),
    materials: input.materials,
    profile: "auto",
    workflow: "bridge",
    eris: input.eris,
    mode: input.mode,
    file_ids: input.file_ids,
  };
}
