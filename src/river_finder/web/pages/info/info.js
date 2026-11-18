/* Info page (#info): what River Finder is, how it works, where the data comes from, current state.
   Also shows a short welcome dialog on a browser's first visit (and again when INFO_VERSION changes).
   Owner: core (beads rf-aq6.6). */
(() => {
  "use strict";
  const RF = window.RF, { el, fmt } = RF.util;
  const INFO_VERSION = "2026-11-18";
  const REPO = "https://github.com/Sean-McConnachie/river-finder";
  const ARC = "https://services3.arcgis.com/fp1tibNcN9mbExhG/ArcGIS/rest/services";

  const link = (href, text) => el("a", { href, target: "_blank", rel: "noopener" }, text);

  const SOURCES = [
    ["Elevation (8 m national DEM, 1 m LiDAR)", "LINZ, via the nz-elevation open data bucket", "Flow routing, catchment area, gradient, terrain shading", "CC BY 4.0",
      "https://registry.opendata.aws/nz-elevation/"],
    ["Satellite imagery (EOxCloudless 2016)", "EOX IT Services; contains modified Copernicus Sentinel data 2016", "Satellite base map (10 m)", "CC BY 4.0",
      "https://cloudless.eox.at"],
    ["NZ Topo50 maps", "LINZ Basemaps", "Topographic base map (standalone site)", "CC BY 4.0", "https://basemaps.linz.govt.nz/"],
    ["River lines and names (NZ River Name Lines, Topo50)", "LINZ Data Service", "The river network on the map and its names", "CC BY 4.0",
      "https://data.linz.govt.nz/layer/103632-nz-river-name-lines-pilot/"],
    ["River network (REC2)", "NIWA (now Earth Sciences NZ)", "Reach names, validation of catchment areas", "CC BY 4.0", `${ARC}/REC2_Layers/FeatureServer/0`],
    ["River and rain gauges", "NIWA flood-statistics gauge sites; NIWA CliFlo stations", "Gauge layers, flood statistics at gauges", "CC BY 4.0",
      `${ARC}/NZ_Flood_Statistics_Henderson_Collins_V2_FlowGauges_Layer_WFL2/FeatureServer/0`],
    ["Flood statistics (Henderson and Collins 2018)", "NIWA (now Earth Sciences NZ)", "Mean annual flood per reach (the flow parameter)", "CC BY 4.0",
      `${ARC}/NZ_Flood_Statistics_Henderson_Collins_V2_REC1_Layer_WFL1/FeatureServer/0`],
    ["Geology 1:250 000 (2023)", "GNS Science (now Earth Sciences NZ)", "Hard-rock beds", "CC BY 4.0",
      "https://gis.gns.cri.nz/server/rest/services/NZL_GNS_250K_Geology_2023/NZL_GNS_250K_geology_FeatureService_NZ/FeatureServer/55"],
    ["Known whitewater sections", "NZ RiverGuide (Whitewater NZ; WWNZ and Graham Charles, NZ Whitewater 4th ed.)", "Known runs, grades, training and test labels",
      "CC BY-SA 3.0 NZ", "https://www.riverguide.co.nz/"],
    ["More sections and beta", "packraftingtrips.nz, FurtherFaster, Ruahine Whitewater Club and others", "Sections not in the river guide (summaries with links)",
      "Owners' copyright (linked only)", "https://www.packraftingtrips.nz/"],
    ["Waterfalls, rapids, roads and tracks", "OpenStreetMap contributors, via Overture Maps", "Waterfall layer and heights, access distances, road names", "ODbL",
      "https://www.openstreetmap.org/copyright"],
  ];

  function section(title, ...body) {
    const id = `info-${title.toLowerCase().replace(/[^a-z]+/g, "-")}`;
    return el("section", { class: "info-section", "aria-labelledby": id }, el("h2", { id, text: title }), ...body);
  }

  function glance() {
    const regions = RF.data.regions(), models = RF.models.list();
    const guide = regions.filter((r) => !/reference/i.test(r.group || "")).length, refs = regions.length - guide;
    const reaches = regions.reduce((t, r) => t + (r.segments || 0), 0);
    const items = [
      ["Regions in this build", regions.length ? `${guide} river-guide regions${refs ? `, ${refs} reference areas` : ""}` : "none (no data)"],
      ["Reaches shown", reaches ? fmt.int(reaches) : "–"],
      ["Known sections", "339 (guide research, Nov 2026)"],
      ["Models", models.length ? `${models.length} (in use: ${RF.models.get(RF.state.model)?.title || RF.state.model || "–"})` : "none in this build"],
    ];
    return el("dl", { class: "info-glance" }, ...items.flatMap(([k, v]) => [el("div", {}, el("dt", { text: k }), el("dd", { text: v }))]));
  }

  function modelTable() {
    const ev = RF.models.evaluations?.evaluations || [];
    const rows = ev.filter((e) => e.split === "guides-v1" && e.coords === "any");
    if (!rows.length) return el("p", { class: "rf-note", text: "No model evaluations in this build." });
    const tb = el("tbody");
    rows.forEach((e) => tb.append(el("tr", {},
      el("td", {}, el("strong", { text: RF.models.get(e.model)?.title || e.model })),
      el("td", { class: "n", text: e.auc.toFixed(2) }), el("td", { class: "n", text: e.auc_size_matched.toFixed(2) }),
      el("td", { class: "n", text: `${Math.round(e.recall_top10 * 100)}%` }))));
    return el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table" },
      el("thead", {}, el("tr", {}, el("th", { text: "Model" }), el("th", { class: "n", text: "AUC" }), el("th", { class: "n", text: "Size-matched AUC" }),
        el("th", { class: "n", text: "Top-10% recall" }))), tb));
  }

  function sourcesTable() {
    const tb = el("tbody");
    SOURCES.forEach(([what, who, use, lic, href]) => tb.append(el("tr", {},
      el("td", {}, link(href, what)), el("td", { text: who }), el("td", { text: use }), el("td", { text: lic }))));
    return el("div", { class: "rf-tablewrap" }, el("table", { class: "rf-table info-sources" },
      el("thead", {}, el("tr", {}, ...["Data", "From", "Used for", "Licence"].map((h) => el("th", { text: h })))), tb));
  }

  const STEPS = [
    ["Terrain", "LINZ elevation models: the 8 m national DEM everywhere, 5 m from 1 m LiDAR in three reference areas (Kaimai Range, Karangahake Gorge, Tutamoe plateau)."],
    ["Flow", "Every cell drains to its steepest neighbour (Priority-Flood, D8, flat resolution). Summing upstream cells gives the catchment area of every point, checked against NIWA's REC2."],
    ["Reaches", "Channels over 0.5 km² are cut into reaches of about 250 m, each with gradient, steepest 50 m, catchment area, mean annual flood, height-to-area and geology."],
    ["Scores", "Models trained on the known guide sections score every reach as a national percentile. Rivers held out of training measure how well each model generalises."],
    ["Runs", "Consecutive high-scoring reaches join into runs. Runs not in any guide are the leads to look at."],
  ];

  function render(root) {
    const C = RF.config;
    root.replaceChildren(el("div", { class: "rf-doc info-doc" },
      el("header", { class: "info-hero" },
        el("span", { class: "rf-eyebrow", text: "About" }),
        el("h1", { text: "River Finder" }),
        el("p", { class: "info-lede", text: "Finds steep, runnable whitewater in New Zealand that is not in any guidebook, from terrain, flood statistics and geology, using the rivers already in the guides as the reference." }),
        el("div", { class: "rf-row" },
          el("button", { type: "button", class: "rf-btn rf-btn--primary", onclick: () => RF.go("map") }, "Open the map"),
          el("button", { type: "button", class: "rf-btn", onclick: () => RF.go("top") }, "See top rivers")),
        glance()),
      el("aside", { class: "info-safety", role: "note" },
        el("strong", { text: "Leads, not trip reports." }),
        el("span", { text: " Candidates come from terrain data only. Check access, land ownership, hazards, flows and local knowledge before going anywhere. Nothing here says a river is safe or runnable." })),
      section("How it works", el("ol", { class: "info-steps" },
        ...STEPS.map(([t, d]) => el("li", {}, el("strong", { text: t }), el("span", { text: d }))))),
      section("Using the app",
        el("ul", { class: "info-list" },
          el("li", {}, el("strong", { text: "Map: " }), "pick a base map (terrain, topographic, satellite, catchment area) and switch layers on and off: known rivers by grade, predicted runs by score, the LINZ river network, gauges, waterfalls, roads and tracks. Tap anything for its details."),
          el("li", {}, el("strong", { text: "Top rivers: " }), "the best-scoring runs for the current model, each one a tap away on the map."),
          el("li", {}, el("strong", { text: "Models: " }), "which model is in use, how each does on rivers it never saw, and the run-finder settings."))),
      section("Current state",
        el("p", { class: "rf-note", html: `Models are tested on 49 guide rivers held out of training. <b>Flood size alone</b> already ranks guide rivers high (AUC 0.92 against every reach of 2 km² or more), so the <b>size-matched AUC</b>, which compares each test reach only with reaches of similar flood size, is the number that shows real terrain skill (0.5 = chance).` }),
        modelTable(),
        el("ul", { class: "info-list" },
          el("li", { text: "Catchment areas: named reference rivers within 3.2% of NIWA REC2; 70–82% of reaches over 5 km² within 10% (target 90%, not met yet: flat plateau divides and floodplain offsets on the 8 m contour DEM)." }),
          el("li", { text: "Known sections: 339 from the guide research; 301 take-outs were inferred from directions, so section extents are approximate." }),
          el("li", { text: "Waterfalls: OpenStreetMap coverage is patchy. No mapped waterfall does not mean no waterfall." }),
          el("li", { text: "Flow: mean annual flood is a statistic, not live flow. Gauge links point to the sources; live readings are planned." }))),
      section("Data sources", el("p", { class: "rf-note", text: "Every layer links to its source. Derived data keep the source licence; guide content is CC BY-SA 3.0 NZ." }), sourcesTable()),
      el("footer", { class: "info-foot rf-note" },
        "Built ", el("span", { class: "num", text: C.built || "–" }), C.commit ? [" from ", link(`${REPO}/commit/${C.commit}`, C.commit)] : "",
        ` · ${C.target === "artifact" ? "claude.ai artifact" : "standalone site"} · `, link(REPO, "Source code"))));
  }

  function welcome() {
    const { el: e } = RF.util;
    const body = e("div", { class: "rf-stack info-welcome" },
      e("p", { text: "River Finder looks for steep, runnable whitewater in New Zealand that is not in the guides, using terrain, flood statistics and the known runs." }),
      e("ul", { class: "info-list" },
        e("li", { text: "Map: switch base maps and layers, tap a river for gradient, flow, profile and links." }),
        e("li", { text: "Top rivers: the best-scoring runs for the current model." }),
        e("li", { text: "Models: what each model learned and how well it does." })),
      e("p", { class: "info-safety", html: `<strong>Leads, not trip reports.</strong> Check access, land ownership, hazards and local knowledge before going anywhere.` }));
    RF.dialog({
      title: "Welcome to River Finder", label: "Welcome", body,
      actions: [{ label: "Read the overview", onclick: () => RF.go("info") }, { label: "Explore the map", primary: true, onclick: () => RF.go("map") }],
    });
  }

  RF.on("ready", () => {
    if (RF.store.get("info-seen") === INFO_VERSION) return;
    RF.store.set("info-seen", INFO_VERSION);
    if (RF.state.page !== "info") welcome();
  });

  RF.page("info", {
    title: "Info",
    mount(root) { render(root); RF.on("model", () => render(root)); },
  });
  RF.info = { welcome, version: INFO_VERSION };
})();
