// Layout: the sidebar lists the modes by category; each page lays its own steps out left to right.
import { BadgeCheck, Download, FileText, Flame, EyeOff, LayoutDashboard, MessageSquareLock, ShieldCheck, FileUp, UserCheck } from "lucide-react";
import { NavLink, Route, Routes } from "react-router-dom";
import { TooltipLayer } from "./components/tooltip";
import { clock, int, percent } from "./lib/format";
import Evaluation from "./pages/Evaluation";
import Exposure from "./pages/Exposure";
import Extraction from "./pages/Extraction";
import Findings from "./pages/Findings";
import Guard from "./pages/Guard";
import Overview from "./pages/Overview";
import Redaction from "./pages/Redaction";
import Reports from "./pages/Reports";
import Review from "./pages/Review";
import Scan from "./pages/Scan";
import { isActive, useStore } from "./store";

const NAV = [
  { section: "Workspace", items: [
    { to: "/", label: "New scan", icon: FileUp },
    { to: "/guard", label: "Prompt guard", icon: MessageSquareLock },
  ] },
  { section: "Analytics", items: [
    { to: "/overview", label: "Overview", icon: LayoutDashboard },
    { to: "/exposure", label: "Exposure & risk", icon: Flame },
    { to: "/findings", label: "Findings", icon: ShieldCheck },
  ] },
  { section: "Documents", items: [
    { to: "/extraction", label: "Extraction", icon: FileText },
    { to: "/redaction", label: "Redaction", icon: EyeOff },
  ] },
  { section: "Assurance", items: [
    { to: "/review", label: "Review", icon: UserCheck },
    { to: "/evaluation", label: "Evaluation", icon: BadgeCheck },
    { to: "/reports", label: "Reports", icon: Download },
  ] },
];

function RunCard() {
  const { run, scan } = useStore();
  if (isActive(scan)) {
    return (
      <NavLink to={scan.kind === "review" ? "/review?step=3" : "/?step=3"} className="side-card live">
        <b>{scan.kind === "review" ? "Applying review" : scan.state === "paused" ? "Scan paused" : "Scan in progress"}</b>
        <div className="progress small"><i style={{ width: percent(scan.fraction, 1) }} className={scan.state === "paused" ? "paused" : ""} /></div>
        <div className="side-row"><span>{percent(scan.fraction)}</span><span>{clock(scan.elapsed)}</span></div>
      </NavLink>
    );
  }
  if (!run) return <div className="side-card"><b>No scan yet</b><br />Start one under Workspace.</div>;
  const review = run.findings.filter((f) => f.decision === "review").length;
  return (
    <div className="side-card">
      <b>Current run</b>
      <div className="side-row"><span>Run</span><code>{run.run_id}</code></div>
      <div className="side-row"><span>Files</span><b>{run.files.length}</b></div>
      <div className="side-row"><span>Findings</span><b>{int(run.findings.length)}</b></div>
      <div className="side-row"><span>Review queue</span><b>{int(review)}</b></div>
      <div className="side-row"><span>Errors</span><b>{Object.keys(run.errors).length}</b></div>
    </div>
  );
}

export default function App() {
  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/shield.svg" alt="" width={26} height={30} />
          PII Shield
        </div>
        <nav>
          {NAV.map((g) => (
            <div key={g.section}>
              <div className="nav-section">{g.section}</div>
              {g.items.map(({ to, label, icon: Icon }) => (
                <NavLink key={to} to={to} end className={({ isActive: on }) => (on ? "nav-link active" : "nav-link")}>
                  <Icon size={17} strokeWidth={1.8} /> {label}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <RunCard />
        <p className="side-note">Offline and fail-closed. No data leaves this machine.</p>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Scan />} />
          <Route path="/guard" element={<Guard />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/exposure" element={<Exposure />} />
          <Route path="/findings" element={<Findings />} />
          <Route path="/extraction" element={<Extraction />} />
          <Route path="/redaction" element={<Redaction />} />
          <Route path="/review" element={<Review />} />
          <Route path="/evaluation" element={<Evaluation />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="*" element={<Scan />} />
        </Routes>
      </main>
      <TooltipLayer />
    </div>
  );
}
