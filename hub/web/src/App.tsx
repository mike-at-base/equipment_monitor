import { useEffect, useState } from "react";
import { BrowserRouter, Link, Route, Routes, useParams } from "react-router-dom";
import { LiveEM, streamLive } from "./api";
import { WindowPicker, WindowProvider } from "./components/ui";
import Site from "./pages/Site";
import Line from "./pages/Line";
import EMPage from "./pages/EM";
import Schedule from "./pages/Schedule";
import Station from "./pages/Station";
import LineModel from "./pages/LineModel";
import Sim from "./pages/Sim";
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
            <Link to="/sim" className="topbar-link">Simulator</Link>
            <WindowPicker />
            <span className={`conn-dot ${connected ? "ok" : ""}`} />
            <span className="conn-label">{connected ? "live" : "reconnecting"}</span>
          </header>
          <Routes>
            <Route path="/" element={<Site live={live} />} />
            <Route path="/line/:line" element={<Line live={live} />} />
            <Route path="/line/:line/schedule" element={<Schedule />} />
            <Route path="/line/:line/model" element={<LineModel />} />
            <Route path="/line/:line/station/:station" element={<Station live={live} />} />
            <Route path="/em/:line/:station/:label/*" element={<EMPage />} />
            <Route path="/sim" element={<Sim live={live} />} />
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
      <Route path="/line/:line/model" element={<NamedCrumb name="Availability model" />} />
      <Route path="/line/:line/station/:station" element={<StationCrumb />} />
      <Route path="/em/:line/:station/:label/*" element={<EMCrumb />} />
      <Route path="/sim" element={
        <nav className="crumbs"><Link to="/">Site</Link><span>/</span><b>Simulator</b></nav>
      } />
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
