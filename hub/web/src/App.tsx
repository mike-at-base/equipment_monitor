import { useEffect, useRef, useState } from "react";
import { BrowserRouter, Link, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";
import { LiveEM, streamLive } from "./api";
import { WindowPicker, WindowProvider } from "./components/ui";
import Site from "./pages/Site";
import Line from "./pages/Line";
import EMPage from "./pages/EM";
import Schedule from "./pages/Schedule";
import Station from "./pages/Station";
import LineModel from "./pages/LineModel";
import Availability from "./pages/Availability";
import "./theme.css";

// The selected window lives in the URL (?win=8h, ?win=custom:<iso>|<iso>)
// so a range can be shared and survives a reload.
//
// The URL is authoritative when it carries the param — a pasted link, the
// back button, or a share all win. When it does NOT, an in-app <Link>
// dropped the query (none of them carry search), so we restore the last
// selection from a ref instead of silently snapping back to "today".
// Doing it this way avoids threading the query string through all 18 links.
function useWindowState() {
  const location = useLocation();
  const navigate = useNavigate();
  const urlWin = new URLSearchParams(location.search).get("win");
  const last = useRef(urlWin || "today");

  const write = (w: string, path: string, search: string) => {
    const next = new URLSearchParams(search);
    if (w === "today") next.delete("win");
    else next.set("win", w);
    const qs = next.toString();
    navigate({ pathname: path, search: qs ? `?${qs}` : "" }, { replace: true });
  };

  useEffect(() => {
    if (urlWin) {
      last.current = urlWin;      // URL wins: paste / back / share
    } else if (last.current !== "today") {
      write(last.current, location.pathname, location.search); // restore
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.pathname, urlWin]);

  // fall back to the ref so the data does not flicker to "today" during the
  // one render between navigating and the URL being rewritten
  const win = urlWin || last.current;
  const setWin = (w: string) => {
    last.current = w;
    write(w, location.pathname, location.search);
  };
  return { win, setWin };
}

export default function App() {
  const [live, setLive] = useState<LiveEM[]>([]);
  const [connected, setConnected] = useState(false);

  useEffect(() => streamLive(setLive, setConnected), []);

  return (
    <BrowserRouter>
      <Shell live={live} connected={connected} />
    </BrowserRouter>
  );
}

// inside the Router so it can read/write the location
function Shell({ live, connected }: { live: LiveEM[]; connected: boolean }) {
  const { win, setWin } = useWindowState();
  return (
      <WindowProvider value={{ win, setWin }}>
        <div className="shell">
          <header className="topbar">
            <Link to="/" className="brandmark">
              <span className="stamp">BASE</span>
              <span className="divider" />
              <span>Equipment Monitor</span>
            </Link>
            <Crumbs />
            <span className="spacer" />
            <WindowPicker />
            <span className={`conn-dot ${connected ? "ok" : ""}`} />
            <span className="conn-label">{connected ? "live" : "reconnecting"}</span>
          </header>
          <Routes>
            <Route path="/" element={<Site live={live} />} />
            <Route path="/line/:line" element={<Line live={live} />} />
            <Route path="/line/:line/schedule" element={<Schedule />} />
            <Route path="/line/:line/model" element={<LineModel />} />
            <Route path="/line/:line/availability" element={<Availability />} />
            <Route path="/line/:line/station/:station" element={<Station live={live} />} />
            <Route path="/em/:line/:station/:label/*" element={<EMPage />} />
          </Routes>
        </div>
      </WindowProvider>
  );
}

function Crumbs() {
  return (
    <Routes>
      <Route path="/" element={null} />
      <Route path="/line/:line" element={<LineCrumb />} />
      <Route path="/line/:line/schedule" element={<ScheduleCrumb />} />
      <Route path="/line/:line/model" element={<NamedCrumb name="Availability model" />} />
      <Route path="/line/:line/availability" element={<NamedCrumb name="Availability" />} />
      <Route path="/line/:line/station/:station" element={<StationCrumb />} />
      <Route path="/em/:line/:station/:label/*" element={<EMCrumb />} />
    </Routes>
  );
}

function LineCrumb() {
  const { line } = useParams();
  return (
    <nav className="crumbs">
      <Link to="/">Site</Link><span>/</span><b>{line}</b>
    </nav>
  );
}

function ScheduleCrumb() {
  const { line } = useParams();
  return (
    <nav className="crumbs">
      <Link to="/">Site</Link><span>/</span>
      <Link to={`/line/${line}`}>{line}</Link><span>/</span><b>Schedule</b>
    </nav>
  );
}

function NamedCrumb({ name }: { name: string }) {
  const { line } = useParams();
  return (
    <nav className="crumbs">
      <Link to="/">Site</Link><span>/</span>
      <Link to={`/line/${line}`}>{line}</Link><span>/</span><b>{name}</b>
    </nav>
  );
}

function StationCrumb() {
  const { line, station } = useParams();
  return (
    <nav className="crumbs">
      <Link to="/">Site</Link><span>/</span>
      <Link to={`/line/${line}`}>{line}</Link><span>/</span><b>{station}</b>
    </nav>
  );
}

function EMCrumb() {
  const { line, station, label } = useParams();
  return (
    <nav className="crumbs">
      <Link to="/">Site</Link><span>/</span>
      <Link to={`/line/${line}`}>{line}</Link><span>/</span>
      <b>{station}{label !== "main" ? ` · ${label}` : ""}</b>
    </nav>
  );
}
