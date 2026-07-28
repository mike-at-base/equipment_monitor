import { useEffect, useState } from "react";
import { BrowserRouter, Link, Route, Routes, useParams } from "react-router-dom";
import { LiveEM, streamLive } from "./api";
import { WindowPicker, WindowProvider } from "./components/ui";
import Site from "./pages/Site";
import Line from "./pages/Line";
import EMPage from "./pages/EM";
import Schedule from "./pages/Schedule";
import "./theme.css";

export default function App() {
  const [live, setLive] = useState<LiveEM[]>([]);
  const [connected, setConnected] = useState(false);
  const [win, setWin] = useState("today");

  useEffect(() => streamLive(setLive, setConnected), []);

  return (
    <BrowserRouter>
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
            <Route path="/em/:line/:station/:label/*" element={<EMPage />} />
          </Routes>
        </div>
      </WindowProvider>
    </BrowserRouter>
  );
}

function Crumbs() {
  return (
    <Routes>
      <Route path="/" element={null} />
      <Route path="/line/:line" element={<LineCrumb />} />
      <Route path="/line/:line/schedule" element={<ScheduleCrumb />} />
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
