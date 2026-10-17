const MAX_TITLE_WORDS = 14;
const MAX_TITLE_CHARS = 96;

function plainLine(line: string): string {
  return line
    .replace(/^\s*(?:#{1,6}\s*|>\s*|[-*+]\s+|\d+[.)]\s+)/, "")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/[*_`~]/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

function usefulLine(line: string): boolean {
  return (
    line.length > 2 &&
    !/^(?:instructions?|background|context|overview|requirements|your task|task|question|challenge|problem|objective|goal|research brief)\s*:?[.!]?$/i.test(
      line,
    ) &&
    !/^(?:you are (?:an?|the)\b|act as\b|your role\b|system prompt\b|```|---+$)/i.test(
      line,
    )
  );
}

/** Extract a label from the user's words; never summarize with invented claims. */
export function researchTitle(request: string): string {
  const copiedHeading = /^(.{12,120}?)\s+Editor\s+View\s+\1(?:\s|$)/i.exec(
    request.trim().replace(/\s+/g, " "),
  )?.[1];
  const rawLines = request.trim().split(/\r?\n/).slice(0, 60);
  const lines = rawLines.map(plainLine).filter(usefulLine);
  const labelled = lines
    .map((line) =>
      /^(?:title|research (?:question|topic)|question|goal|objective|challenge|task|problem)\s*[:—–]\s*(.+)$/i.exec(
        line,
      ),
    )
    .find((match) => match && usefulLine(match[1]));
  const heading = rawLines
    .filter((line) => /^\s*#{1,6}\s+/.test(line))
    .map(plainLine)
    .find(usefulLine);
  const selected =
    copiedHeading || labelled?.[1] || heading || lines[0] || "Research session";
  const sentence = selected.match(/^.*?[.!?](?=\s|$)/)?.[0] || selected;
  const words = sentence.split(/\s+/);
  let title = words.slice(0, MAX_TITLE_WORDS).join(" ");
  let shortened = words.length > MAX_TITLE_WORDS;
  if ([...title].length > MAX_TITLE_CHARS - (shortened ? 1 : 0)) {
    title = [...title].slice(0, MAX_TITLE_CHARS - 1).join("");
    const boundary = title.lastIndexOf(" ");
    if (boundary > MAX_TITLE_CHARS / 2) title = title.slice(0, boundary);
    shortened = true;
  }
  return shortened ? title.replace(/[\s,;:.!?—–-]+$/u, "") + "…" : title;
}
