/**
 * FillMyPDF — interactive feature matrix viewer (React).
 *
 * Data lives in ./feature-matrix.json (regenerate with:
 *   python3 scripts/feature_matrix_snapshot.py
 * )
 */
import React, { useMemo, useState } from "react";
import matrix from "./feature-matrix.json";

const categories = matrix.categories;
const seedFeatures = matrix.features;

const FeatureMatrix = () => {
  const [activeView, setActiveView] = useState("matrix");
  const [filterCategory, setFilterCategory] = useState("all");
  const [filterPriority, setFilterPriority] = useState("all");
  const [selectedFeature, setSelectedFeature] = useState(null);

  const allFeatures = useMemo(() => seedFeatures, []);

  const filteredFeatures = useMemo(
    () =>
      allFeatures.filter((f) => {
        if (filterCategory !== "all" && f.category !== filterCategory) return false;
        if (filterPriority !== "all" && f.priority !== filterPriority) return false;
        return true;
      }),
    [allFeatures, filterCategory, filterPriority]
  );

  const Badge = ({ children, color = "gray" }) => {
    const colors = {
      blue: "bg-blue-500/20 text-blue-300",
      green: "bg-green-500/20 text-green-300",
      purple: "bg-purple-500/20 text-purple-300",
      orange: "bg-orange-500/20 text-orange-300",
      red: "bg-red-500/20 text-red-300",
      yellow: "bg-yellow-500/20 text-yellow-300",
      pink: "bg-pink-500/20 text-pink-300",
      teal: "bg-teal-500/20 text-teal-300",
      gray: "bg-gray-500/20 text-gray-300",
    };
    return (
      <span className={`px-2 py-1 rounded text-xs font-medium ${colors[color] ?? colors.gray}`}>{children}</span>
    );
  };

  const getStatusColor = (status) => {
    switch (status) {
      case "done":
        return "green";
      case "build":
        return "blue";
      case "later":
        return "orange";
      case "skip":
        return "red";
      default:
        return "gray";
    }
  };

  const getStatusLabel = (status) => {
    switch (status) {
      case "done":
        return "✅ Done";
      case "build":
        return "🚀 Build";
      case "later":
        return "⏳ Later";
      case "skip":
        return "❌ Skip";
      default:
        return status;
    }
  };

  const phaseStyle = (phase) => {
    const map = {
      0: "bg-green-500/10 border border-green-400/30",
      1: "bg-green-500/10 border border-green-400/30",
      2: "bg-blue-500/10 border border-blue-400/30",
      3: "bg-purple-500/10 border border-purple-400/30",
      4: "bg-orange-500/10 border border-orange-400/30",
      5: "bg-red-500/10 border border-red-400/30",
    };
    return map[phase] ?? "bg-slate-700/40 border border-white/10";
  };

  const phaseTitle = (phase) => {
    const labels = {
      0: { label: "Already Done", icon: "✅" },
      1: { label: "Phase 1: Foundation (Month 1-2)", icon: "🏗️" },
      2: { label: "Phase 2: Automation (Month 3-4)", icon: "⚡" },
      3: { label: "Phase 3: Signing (Month 5-6)", icon: "✍️" },
      4: { label: "Phase 4: Enterprise (Month 7-9)", icon: "🏢" },
      5: { label: "Phase 5: Platform (Month 10-12)", icon: "🚀" },
    };
    return labels[phase] ?? { label: `Phase ${phase}`, icon: "•" };
  };

  return (
    <div className="min-h-screen bg-slate-900 text-white">
      <div className="bg-gradient-to-r from-indigo-600/20 to-purple-600/20 border-b border-white/10">
        <div className="max-w-7xl mx-auto px-6 py-8">
          <h1 className="text-4xl font-bold mb-2">📋 FillMyPDF Feature Matrix</h1>
          <p className="text-xl text-gray-300">
            Source: <code className="text-indigo-300">docs/feature-matrix.json</code>
          </p>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="grid grid-cols-5 gap-4">
          <div className="bg-green-500/20 rounded-xl p-4 text-center border border-green-400/30">
            <div className="text-3xl font-bold text-green-400">{allFeatures.filter((f) => f.status === "done").length}</div>
            <div className="text-sm text-gray-400">Done</div>
          </div>
          <div className="bg-blue-500/20 rounded-xl p-4 text-center border border-blue-400/30">
            <div className="text-3xl font-bold text-blue-400">{allFeatures.filter((f) => f.status === "build").length}</div>
            <div className="text-sm text-gray-400">Build Next</div>
          </div>
          <div className="bg-orange-500/20 rounded-xl p-4 text-center border border-orange-400/30">
            <div className="text-3xl font-bold text-orange-400">{allFeatures.filter((f) => f.status === "later").length}</div>
            <div className="text-sm text-gray-400">Defer</div>
          </div>
          <div className="bg-red-500/20 rounded-xl p-4 text-center border border-red-400/30">
            <div className="text-3xl font-bold text-red-400">{allFeatures.filter((f) => f.status === "skip").length}</div>
            <div className="text-sm text-gray-400">Skip</div>
          </div>
          <div className="bg-purple-500/20 rounded-xl p-4 text-center border border-purple-400/30">
            <div className="text-3xl font-bold text-purple-400">{allFeatures.length}</div>
            <div className="text-sm text-gray-400">Total</div>
          </div>
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-6">
        <div className="flex gap-2 mb-6 flex-wrap">
          {[
            { id: "matrix", label: "📊 Priority Matrix" },
            { id: "timeline", label: "📅 By Phase" },
            { id: "category", label: "📁 By Category" },
            { id: "list", label: "📋 Full List" },
          ].map((v) => (
            <button
              key={v.id}
              type="button"
              onClick={() => setActiveView(v.id)}
              className={`px-4 py-2 rounded-lg font-medium transition-all ${
                activeView === v.id ? "bg-white text-gray-900" : "bg-white/10 hover:bg-white/20"
              }`}
            >
              {v.label}
            </button>
          ))}
        </div>
      </div>

      <div className="max-w-7xl mx-auto px-6 py-6">
        {activeView === "matrix" && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold">Impact vs Effort Matrix</h2>
            <div className="grid grid-cols-2 gap-4">
              <div className="bg-green-500/10 rounded-xl p-6 border border-green-400/30">
                <h3 className="font-bold text-green-400 text-lg mb-4">🚀 Quick Wins (DO FIRST)</h3>
                <p className="text-sm text-gray-400 mb-4">Low effort, High impact</p>
                <div className="space-y-2 max-h-[420px] overflow-y-auto pr-1">
                  {allFeatures
                    .filter((f) => f.effort <= 2 && f.impact >= 4)
                    .sort((a, b) => b.impact * b.revenue - a.impact * a.revenue)
                    .map((f) => (
                      <button
                        type="button"
                        key={f.id}
                        className="bg-white/5 rounded-lg p-3 w-full text-left hover:bg-white/10"
                        onClick={() => setSelectedFeature(f)}
                      >
                        <div className="flex justify-between items-center gap-2">
                          <span className="font-medium">{f.name}</span>
                          <Badge color={getStatusColor(f.status)}>{getStatusLabel(f.status)}</Badge>
                        </div>
                        <div className="text-xs text-gray-500 mt-1">
                          Phase {f.phase ?? "-"} • Effort: {f.effort} • Impact: {f.impact}
                        </div>
                      </button>
                    ))}
                </div>
              </div>

              <div className="bg-blue-500/10 rounded-xl p-6 border border-blue-400/30">
                <h3 className="font-bold text-blue-400 text-lg mb-4">🎯 Major Projects (PLAN CAREFULLY)</h3>
                <p className="text-sm text-gray-400 mb-4">High effort, High impact</p>
                <div className="space-y-2 max-h-[420px] overflow-y-auto pr-1">
                  {allFeatures
                    .filter((f) => f.effort >= 3 && f.impact >= 4)
                    .sort((a, b) => b.impact * b.revenue - a.impact * a.revenue)
                    .map((f) => (
                      <button
                        type="button"
                        key={f.id}
                        className="bg-white/5 rounded-lg p-3 w-full text-left hover:bg-white/10"
                        onClick={() => setSelectedFeature(f)}
                      >
                        <div className="flex justify-between items-center gap-2">
                          <span className="font-medium">{f.name}</span>
                          <Badge color={getStatusColor(f.status)}>{getStatusLabel(f.status)}</Badge>
                        </div>
                        <div className="text-xs text-gray-500 mt-1">
                          Phase {f.phase ?? "-"} • Effort: {f.effort} • Impact: {f.impact}
                        </div>
                      </button>
                    ))}
                </div>
              </div>

              <div className="bg-gray-500/10 rounded-xl p-6 border border-gray-400/30">
                <h3 className="font-bold text-gray-400 text-lg mb-4">📦 Fill-ins (IF TIME PERMITS)</h3>
                <p className="text-sm text-gray-400 mb-4">Low effort, Lower impact</p>
                <div className="space-y-2 max-h-[380px] overflow-y-auto">
                  {allFeatures
                    .filter((f) => f.effort <= 2 && f.impact < 4 && f.status !== "skip")
                    .map((f) => (
                      <button
                        type="button"
                        key={f.id}
                        className="bg-white/5 rounded-lg p-3 w-full text-left hover:bg-white/10"
                        onClick={() => setSelectedFeature(f)}
                      >
                        <div className="flex justify-between items-center gap-2">
                          <span className="font-medium">{f.name}</span>
                          <Badge color={getStatusColor(f.status)}>{getStatusLabel(f.status)}</Badge>
                        </div>
                        <div className="text-xs text-gray-500 mt-1">
                          Phase {f.phase ?? "-"} • Effort: {f.effort} • Impact: {f.impact}
                        </div>
                      </button>
                    ))}
                </div>
              </div>

              <div className="bg-red-500/10 rounded-xl p-6 border border-red-400/30">
                <h3 className="font-bold text-red-400 text-lg mb-4">⚠️ Time Sinks (AVOID)</h3>
                <p className="text-sm text-gray-400 mb-4">High effort, Lower impact OR Skip</p>
                <div className="space-y-2 max-h-[380px] overflow-y-auto">
                  {allFeatures
                    .filter((f) => (f.effort >= 3 && f.impact < 4) || f.status === "skip")
                    .map((f) => (
                      <button
                        type="button"
                        key={f.id}
                        className="bg-white/5 rounded-lg p-3 w-full text-left hover:bg-white/10"
                        onClick={() => setSelectedFeature(f)}
                      >
                        <div className="flex justify-between items-center gap-2">
                          <span className="font-medium">{f.name}</span>
                          <Badge color={getStatusColor(f.status)}>{getStatusLabel(f.status)}</Badge>
                        </div>
                        <div className="text-xs text-gray-500 mt-1">
                          {f.status === "skip" ? "Not aligned with focus (per matrix)" : `Effort: ${f.effort} • Impact: ${f.impact}`}
                        </div>
                      </button>
                    ))}
                </div>
              </div>
            </div>
          </div>
        )}

        {activeView === "timeline" && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold">Features by Phase</h2>
            {[0, 1, 2, 3, 4, 5].map((phase) => {
              const phaseFeatures = allFeatures.filter((f) => f.phase === phase);
              if (phaseFeatures.length === 0) return null;
              const p = phaseTitle(phase);
              return (
                <div key={phase} className={`rounded-xl p-6 ${phaseStyle(phase)}`}>
                  <h3 className="font-bold text-xl mb-4 text-white">
                    {p.icon} {p.label}
                  </h3>
                  <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
                    {phaseFeatures.map((f) => (
                      <button
                        type="button"
                        key={f.id}
                        className="bg-white/5 rounded-lg p-4 text-left hover:bg-white/10"
                        onClick={() => setSelectedFeature(f)}
                      >
                        <div className="flex items-start justify-between mb-2 gap-2">
                          <span className="font-medium">{f.name}</span>
                          <Badge color={f.priority === "critical" ? "red" : f.priority === "high" ? "orange" : "gray"}>
                            {f.priority}
                          </Badge>
                        </div>
                        <p className="text-sm text-gray-400">{f.description}</p>
                        <div className="flex items-center gap-2 mt-3 text-xs flex-wrap">
                          <Badge color={categories[f.category].color}>{categories[f.category].label}</Badge>
                          <span className="text-gray-500">Effort: {f.effort}/5</span>
                        </div>
                      </button>
                    ))}
                  </div>
                </div>
              );
            })}

            <div className="bg-red-500/10 rounded-xl p-6 border border-red-400/30">
              <h3 className="font-bold text-red-400 text-xl mb-4">❌ Not Building (Skip)</h3>
              <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
                {allFeatures
                  .filter((f) => f.status === "skip")
                  .map((f) => (
                    <button
                      type="button"
                      key={f.id}
                      className="bg-white/5 rounded-lg p-4 text-left hover:bg-white/10"
                      onClick={() => setSelectedFeature(f)}
                    >
                      <span className="font-medium">{f.name}</span>
                      <p className="text-sm text-gray-500 mt-1">{f.details?.whySkip || "Not aligned with focus"}</p>
                    </button>
                  ))}
              </div>
            </div>
          </div>
        )}

        {activeView === "category" && (
          <div className="space-y-6">
            <h2 className="text-2xl font-bold">Features by Category</h2>
            {Object.entries(categories).map(([catId, cat]) => {
              const catFeatures = allFeatures.filter((f) => f.category === catId);
              if (catFeatures.length === 0) return null;

              const borderMap = {
                blue: "border-blue-400/30",
                green: "border-green-400/30",
                purple: "border-purple-400/30",
                pink: "border-pink-400/30",
                orange: "border-orange-400/30",
                red: "border-red-400/30",
                teal: "border-teal-400/30",
                gray: "border-gray-400/30",
              };

              return (
                <div key={catId} className={`bg-white/5 rounded-xl p-6 border ${borderMap[cat.color] ?? borderMap.gray}`}>
                  <h3 className="font-bold text-xl mb-4 text-white">
                    {cat.icon} {cat.label} ({catFeatures.length})
                  </h3>
                  <div className="grid md:grid-cols-2 gap-3">
                    {catFeatures
                      .slice()
                      .sort((a, b) => {
                        const statusOrder = { done: 0, build: 1, later: 2, skip: 3 };
                        return statusOrder[a.status] - statusOrder[b.status];
                      })
                      .map((f) => (
                        <button
                          type="button"
                          key={f.id}
                          className="bg-white/5 rounded-lg p-4 text-left hover:bg-white/10"
                          onClick={() => setSelectedFeature(f)}
                        >
                          <div className="flex items-start justify-between mb-2 gap-2">
                            <span className="font-medium">{f.name}</span>
                            <Badge color={getStatusColor(f.status)}>{getStatusLabel(f.status)}</Badge>
                          </div>
                          <p className="text-sm text-gray-400">{f.description}</p>
                          {f.phase != null ? <div className="text-xs text-gray-500 mt-2">Phase {f.phase}</div> : null}
                        </button>
                      ))}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {activeView === "list" && (
          <div className="space-y-4">
            <div className="flex gap-4 mb-6 flex-wrap">
              <select
                value={filterCategory}
                onChange={(e) => setFilterCategory(e.target.value)}
                className="bg-white/10 rounded-lg px-4 py-2 text-white border border-white/10"
              >
                <option value="all">All Categories</option>
                {Object.entries(categories).map(([id, cat]) => (
                  <option key={id} value={id}>
                    {cat.label}
                  </option>
                ))}
              </select>
              <select
                value={filterPriority}
                onChange={(e) => setFilterPriority(e.target.value)}
                className="bg-white/10 rounded-lg px-4 py-2 text-white border border-white/10"
              >
                <option value="all">All Priorities</option>
                <option value="critical">Critical</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
            </div>

            <div className="bg-white/5 rounded-xl overflow-hidden border border-white/10">
              <table className="w-full text-sm">
                <thead className="bg-white/10">
                  <tr>
                    <th className="text-left p-4">Feature</th>
                    <th className="text-left p-4">Category</th>
                    <th className="text-center p-4">Priority</th>
                    <th className="text-center p-4">Effort</th>
                    <th className="text-center p-4">Impact</th>
                    <th className="text-center p-4">Phase</th>
                    <th className="text-left p-4">Status</th>
                  </tr>
                </thead>
                <tbody>
                  {filteredFeatures.map((f) => (
                    <tr
                      key={f.id}
                      className="border-t border-white/5 cursor-pointer hover:bg-white/5"
                      onClick={() => setSelectedFeature(f)}
                    >
                      <td className="p-4">
                        <div className="font-medium">{f.name}</div>
                        <div className="text-xs text-gray-500 mt-1">{f.description}</div>
                      </td>
                      <td className="p-4">
                        <Badge color={categories[f.category].color}>
                          {categories[f.category].icon} {categories[f.category].label}
                        </Badge>
                      </td>
                      <td className="p-4 text-center">
                        <Badge
                          color={
                            f.priority === "critical" ? "red" : f.priority === "high" ? "orange" : f.priority === "medium" ? "yellow" : "gray"
                          }
                        >
                          {f.priority}
                        </Badge>
                      </td>
                      <td className="p-4 text-center">
                        <div className="flex justify-center">
                          {Array.from({ length: 5 }).map((_, i) => (
                            <div key={i} className={`w-2 h-2 rounded-full mx-0.5 ${i < f.effort ? "bg-red-400" : "bg-gray-600"}`} />
                          ))}
                        </div>
                      </td>
                      <td className="p-4 text-center">
                        <div className="flex justify-center">
                          {Array.from({ length: 5 }).map((_, i) => (
                            <div key={i} className={`w-2 h-2 rounded-full mx-0.5 ${i < f.impact ? "bg-green-400" : "bg-gray-600"}`} />
                          ))}
                        </div>
                      </td>
                      <td className="p-4 text-center">{f.phase != null ? `P${f.phase}` : "-"}</td>
                      <td className="p-4">
                        <Badge color={getStatusColor(f.status)}>{getStatusLabel(f.status)}</Badge>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        )}
      </div>

      {selectedFeature ? (
        <div className="fixed inset-0 bg-black/80 flex items-center justify-center p-4 z-50" onClick={() => setSelectedFeature(null)}>
          <div
            className="bg-slate-800 rounded-2xl max-w-2xl w-full max-h-[90vh] overflow-y-auto border border-white/10"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="p-6 border-b border-white/10">
              <div className="flex justify-between items-start gap-4">
                <div>
                  <h2 className="text-2xl font-bold">{selectedFeature.name}</h2>
                  <p className="text-gray-400 mt-1">{selectedFeature.description}</p>
                </div>
                <button type="button" onClick={() => setSelectedFeature(null)} className="text-gray-400 hover:text-white text-2xl leading-none">
                  ×
                </button>
              </div>

              <div className="flex gap-2 mt-4 flex-wrap">
                <Badge color={getStatusColor(selectedFeature.status)}>{getStatusLabel(selectedFeature.status)}</Badge>
                <Badge color={categories[selectedFeature.category].color}>{categories[selectedFeature.category].label}</Badge>
                <Badge color={selectedFeature.priority === "critical" ? "red" : "orange"}>{selectedFeature.priority} priority</Badge>
                {selectedFeature.phase != null ? <Badge color="purple">Phase {selectedFeature.phase}</Badge> : null}
              </div>
            </div>

            <div className="p-6 space-y-6">
              <div
                className={`p-4 rounded-xl ${
                  selectedFeature.status === "done"
                    ? "bg-green-500/20"
                    : selectedFeature.status === "build"
                      ? "bg-blue-500/20"
                      : selectedFeature.status === "later"
                        ? "bg-orange-500/20"
                        : "bg-red-500/20"
                }`}
              >
                <div className="font-bold text-lg">{selectedFeature.recommendation}</div>
              </div>

              <div className="grid grid-cols-3 gap-4">
                <div className="bg-white/5 rounded-lg p-4 text-center">
                  <div className="text-2xl font-bold text-red-400">{selectedFeature.effort}/5</div>
                  <div className="text-sm text-gray-400">Effort</div>
                </div>
                <div className="bg-white/5 rounded-lg p-4 text-center">
                  <div className="text-2xl font-bold text-green-400">{selectedFeature.impact}/5</div>
                  <div className="text-sm text-gray-400">Impact</div>
                </div>
                <div className="bg-white/5 rounded-lg p-4 text-center">
                  <div className="text-2xl font-bold text-yellow-400">{selectedFeature.revenue}/5</div>
                  <div className="text-sm text-gray-400">Revenue</div>
                </div>
              </div>

              {selectedFeature.details ? (
                <div className="space-y-4">
                  {Object.entries(selectedFeature.details).map(([key, value]) => {
                    if (!value) return null;
                    return (
                      <div key={key} className="bg-white/5 rounded-lg p-4 border border-white/5">
                        <div className="text-sm text-gray-400 mb-2 capitalize">{key.replace(/([A-Z])/g, " $1").trim()}</div>
                        {Array.isArray(value) ? (
                          <ul className="space-y-1">
                            {value.map((v, i) => (
                              <li key={i} className="text-sm">
                                • {String(v)}
                              </li>
                            ))}
                          </ul>
                        ) : typeof value === "object" ? (
                          <div className="space-y-2 text-sm">
                            {Object.entries(value).map(([k, v]) => (
                              <div key={k}>
                                <span className="font-medium">{k}: </span>
                                <span className="text-gray-300">{Array.isArray(v) ? v.join(", ") : String(v)}</span>
                              </div>
                            ))}
                          </div>
                        ) : (
                          <div className="text-sm">{String(value)}</div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ) : null}
            </div>
          </div>
        </div>
      ) : null}

      <div className="max-w-7xl mx-auto px-6 py-8 border-t border-white/10">
        <h2 className="text-2xl font-bold mb-4 text-white">Implementation route (backend repo vs matrix)</h2>
        <p className="text-gray-400 text-sm mb-4">
          Canonical mapping lives in{" "}
          <code className="text-indigo-300 bg-white/10 px-1 rounded">docs/FEATURE_MATRIX_ROUTE.md</code>.
        </p>
      </div>

      <div className="text-center py-6 text-gray-500 text-sm border-t border-white/10">
        <p>
          FillMyPDF • Matrix rows: <code className="text-gray-400">{String(allFeatures.length)}</code> • JSON + React viewer • Click any card for breakdown
        </p>
      </div>
    </div>
  );
};

export default FeatureMatrix;
