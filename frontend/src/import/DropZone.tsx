import { useId, useState } from "react";

/**
 * The only thing on the page until a file exists.
 *
 * The wizard used to open with an account picker beside the file input, which reads
 * as a two-field form and puts the account question first — at the one moment nobody
 * can answer it, since whether the file covers one account or names its own is not
 * yet known. Asking for the file alone lets the file answer.
 */
export function DropZone({ onFile }: { onFile: (f: File) => void }) {
  const id = useId();
  const [over, setOver] = useState(false);

  const take = (files: FileList | null) => {
    const f = files?.[0];
    if (f) onFile(f);
  };

  return (
    <div
      className={`dropzone${over ? " over" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        take(e.dataTransfer.files);
      }}
    >
      <p className="dropzone-lead">Drop a statement here</p>
      {/* The input is the label's control, so the whole button is the target and the
          native file field's own text never appears. */}
      <label className="btn btn-primary" htmlFor={id}>
        Choose a file
      </label>
      <input
        id={id}
        type="file"
        className="sr-only"
        accept=".csv,.ofx,.qfx"
        onChange={(e) => take(e.target.files)}
      />
      <p className="muted dropzone-note">
        CSV, OFX or QFX. If the file says which accounts its transactions belong to,
        Saiva reads that from the file — you will not be asked to choose one.
      </p>
    </div>
  );
}
