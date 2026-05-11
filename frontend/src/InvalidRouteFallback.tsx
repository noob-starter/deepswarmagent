import { useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";

function UnknownRouteIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden>
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M9.879 7.519c1.171-1.025 3.071-1.025 4.242 0 1.172 1.025 1.172 2.687 0 3.712-.203.179-.43.326-.67.442-.745.361-1.45.999-1.45 1.827v.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Zm-9 5.25h.008v.008H12v-.008Z"
      />
    </svg>
  );
}

export function InvalidRouteFallback() {
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    const id = window.setTimeout(() => {
      navigate("/", { replace: true });
    }, 3000);
    return () => window.clearTimeout(id);
  }, [navigate]);

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center p-6 bg-slate-950/85 backdrop-blur-md">
      <div
        role="alert"
        aria-live="polite"
        className="w-full max-w-md rounded-2xl border border-slate-700/80 bg-gradient-to-b from-slate-900/95 to-slate-950/98 px-8 py-10 shadow-[0_25px_50px_-12px_rgba(0,0,0,0.65)] text-center"
      >
        <div className="mx-auto mb-5 flex h-14 w-14 items-center justify-center rounded-full bg-emerald-500/15 text-emerald-400 ring-1 ring-emerald-500/30">
          <UnknownRouteIcon className="h-8 w-8" />
        </div>
        <h1 className="text-lg font-semibold tracking-tight text-white">That address isn&apos;t available</h1>
        <p className="mt-2 text-sm leading-relaxed text-slate-400">
          <span className="font-mono text-amber-200/90">{location.pathname}</span>
          <span className="text-slate-500"> isn&apos;t a page here. Taking you home in a moment.</span>
        </p>
        <div className="mt-8 h-1.5 w-full overflow-hidden rounded-full bg-slate-800/90">
          <div className="h-full origin-left rounded-full bg-gradient-to-r from-emerald-600 to-teal-400 animate-invalid-route-bar" />
        </div>
        <p className="mt-4 text-[11px] uppercase tracking-[0.2em] text-slate-500">Redirecting</p>
      </div>
    </div>
  );
}
