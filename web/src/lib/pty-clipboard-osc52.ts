/**
 * OSC 52 clipboard handling for the embedded terminal.
 *
 * Extracted verbatim from ChatPage (size ratchet): the embedded TUI asks the
 * browser to copy a selection with an OSC 52 escape (base64 payload); writes
 * go to the system clipboard via the shared clipboard helper (which falls back
 * to a selection-based copy when the Clipboard API is unavailable on
 * plain-HTTP deployments, or when the write lands outside the originating
 * keydown's "user gesture" activation). Reads (`?`) are refused — content the
 * TUI renders must never exfiltrate the user's clipboard.
 */

import { copyTextToClipboard } from "@/lib/clipboard";

interface OscParser {
  registerOscHandler(
    ident: number,
    callback: (data: string) => boolean | Promise<boolean>,
  ): void;
}

export function registerClipboardOsc52(parser: OscParser): void {
  parser.registerOscHandler(52, (data) => {
    // Format: "<targets>;<base64 | '?'>"
    const semi = data.indexOf(";");
    if (semi < 0) return false;
    const payload = data.slice(semi + 1);
    if (payload === "?" || payload === "") return false; // read/clear — ignore
    try {
      const binary = atob(payload);
      const bytes = Uint8Array.from(binary, (c) => c.charCodeAt(0));
      const text = new TextDecoder("utf-8").decode(bytes);
      void copyTextToClipboard(text).then((copied) => {
        if (!copied) {
          console.warn("[dashboard clipboard] OSC 52 write failed");
        }
      });
    } catch {
      console.warn("[dashboard clipboard] malformed OSC 52 payload");
    }
    return true;
  });
}
