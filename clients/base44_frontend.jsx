/**
 * Base44 frontend — calls the "appraise" backend function.
 *
 * Notice what is NOT in this file: the API key, the API URL, and any header
 * named X-API-Key. This code ships to the browser, so nothing secret can live
 * here. It calls the backend function, and the backend function holds the key.
 *
 * Paste this into a Base44 page component, or hand the whole file to the
 * Base44 AI chat and ask it to wire it into your app.
 */

import { useState } from "react";
import { functions } from "@base44/sdk";

export default function AppraisalForm() {
  const [subject, setSubject] = useState({
    main_sqft: 1800,
    upper_sqft: 900,
    bsmt_fin_sqft: 800,
    bedrooms: 4,
    baths_full: 3,
    baths_half: 1,
    garage_spaces: 3,
    fireplaces: 1,
    lot_sqft: 12000,
    year_built: 2005,
    location: "Zionsville",
    effective_date: new Date().toISOString().slice(0, 10),
    address: "",
    file_number: "",
  });

  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const setField = (name, value) =>
    setSubject((s) => ({ ...s, [name]: value === "" ? null : value }));

  // ---------------------------------------------------------------- JSON
  async function getValuation() {
    setBusy(true);
    setError(null);
    try {
      const res = await functions.invoke("appraise", {
        subject,
        format: "json",
        scenarios: [
          { feature: "garage_spaces", values: [1, 2, 3] },
          { feature: "baths_full", values: [2, 3, 4] },
        ],
      });
      if (res.error) throw new Error(res.detail || res.error);
      setResult(res);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  // ---------------------------------------------------------------- PDF
  // The browser cannot "save" a response on its own. Turn the bytes into a
  // Blob, make a temporary object URL, click a hidden link, then revoke the
  // URL -- skipping the revoke leaks memory on every report generated.
  async function downloadPdf() {
    setBusy(true);
    setError(null);
    try {
      const res = await functions.invoke("appraise", {
        subject,
        format: "pdf",
        report_title: "Market-Derived Adjustment Analysis",
      }, { responseType: "blob" });

      const blob = res instanceof Blob ? res : new Blob([res], { type: "application/pdf" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `adjustment-analysis-${subject.file_number || "subject"}.pdf`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError(`Could not generate the PDF: ${e.message}`);
    } finally {
      setBusy(false);
    }
  }

  const money = (v) =>
    v == null ? "-" : (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString(undefined, { maximumFractionDigits: 0 });

  const fields = [
    ["main_sqft", "Main level sq ft"],
    ["upper_sqft", "Upper level sq ft"],
    ["bsmt_fin_sqft", "Finished basement sq ft"],
    ["bedrooms", "Bedrooms"],
    ["baths_full", "Full baths"],
    ["baths_half", "Half baths"],
    ["garage_spaces", "Garage bays"],
    ["fireplaces", "Fireplaces"],
    ["lot_sqft", "Lot sq ft"],
    ["year_built", "Year built"],
  ];

  return (
    <div className="max-w-3xl mx-auto p-6 space-y-6">
      <h1 className="text-2xl font-semibold">Subject Property Valuation</h1>

      <div className="grid grid-cols-2 gap-3">
        {fields.map(([name, label]) => (
          <label key={name} className="flex flex-col text-sm">
            <span className="text-slate-600">{label}</span>
            <input
              type="number"
              className="border rounded px-2 py-1"
              value={subject[name] ?? ""}
              onChange={(e) => setField(name, e.target.value === "" ? "" : Number(e.target.value))}
            />
          </label>
        ))}

        <label className="flex flex-col text-sm">
          <span className="text-slate-600">Location</span>
          <input className="border rounded px-2 py-1" value={subject.location ?? ""}
                 onChange={(e) => setField("location", e.target.value)} />
        </label>

        <label className="flex flex-col text-sm">
          <span className="text-slate-600">Effective date</span>
          <input type="date" className="border rounded px-2 py-1" value={subject.effective_date ?? ""}
                 onChange={(e) => setField("effective_date", e.target.value)} />
        </label>

        <label className="flex flex-col text-sm col-span-2">
          <span className="text-slate-600">Address (report only, not a model input)</span>
          <input className="border rounded px-2 py-1" value={subject.address ?? ""}
                 onChange={(e) => setField("address", e.target.value)} />
        </label>
      </div>

      <div className="flex gap-3">
        <button onClick={getValuation} disabled={busy}
                className="px-4 py-2 rounded bg-slate-800 text-white disabled:opacity-50">
          {busy ? "Working..." : "Get valuation"}
        </button>
        <button onClick={downloadPdf} disabled={busy}
                className="px-4 py-2 rounded border border-slate-800 disabled:opacity-50">
          Download PDF report
        </button>
      </div>

      {error && <div className="p-3 rounded bg-red-50 text-red-800 text-sm">{error}</div>}

      {result && (
        <div className="space-y-4">
          <div className="p-4 rounded bg-slate-100">
            <div className="text-xs uppercase text-slate-500">Indicated value</div>
            <div className="text-3xl font-semibold">{money(result.indicated_value)}</div>
            <div className="text-sm text-slate-600">
              Supported range {money(result.value_range_low)} to {money(result.value_range_high)}
            </div>
          </div>

          {/* Surface the warnings. They are the difference between a number the
              appraiser can rely on and one that was quietly pinned to the edge
              of the training data. Do not hide them in a console.log. */}
          {result.warnings?.length > 0 && (
            <ul className="p-3 rounded bg-amber-50 text-amber-900 text-sm list-disc pl-6">
              {result.warnings.map((w, i) => <li key={i}>{w}</li>)}
            </ul>
          )}

          <table className="w-full text-sm">
            <thead className="bg-slate-800 text-white">
              <tr>
                <th className="text-left p-2">Characteristic</th>
                <th className="text-right p-2">Adjustment</th>
                <th className="text-right p-2">95% interval</th>
                <th className="text-left p-2 pl-4">Use in grid</th>
              </tr>
            </thead>
            <tbody>
              {result.adjustment_grid?.map((r) => (
                <tr key={r.feature} className="border-b">
                  <td className="p-2">{r.unit}</td>
                  <td className="p-2 text-right">{money(r.adjustment)}</td>
                  <td className="p-2 text-right text-slate-500">
                    {money(r.ci_lower_95)} to {money(r.ci_upper_95)}
                  </td>
                  <td className={"p-2 pl-4 " + (r.use_in_grid.startsWith("YES")
                    ? "text-green-700" : "text-red-700")}>
                    {r.use_in_grid}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
