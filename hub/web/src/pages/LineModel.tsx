import { useParams } from "react-router-dom";
import { api } from "../api";
import { ModelPanel } from "../components/availmodel";

// Line-scope availability model: how the stations compose into the line
// (default: all stations in series).
export default function LineModel() {
  const { line = "" } = useParams();
  return (
    <div className="card" style={{ marginTop: 16 }}>
      <h2>Line availability model · {line}</h2>
      <p className="muted" style={{ marginTop: 0 }}>
        Defines when the LINE counts as available, composed from each
        station's own availability model. Buffers between stations are not
        modeled — a station outage shorter than a buffer will still show as
        line-down for that time.
      </p>
      <ModelPanel memberNoun="station"
        defaultHint="all stations in series."
        load={() => api.getLineModel(line)}
        save={(m) => api.saveLineModel(line, m)} />
    </div>
  );
}
