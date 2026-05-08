/**
 * app.js – SG Internship Finder frontend logic.
 * No framework dependencies – plain ES2020.
 */

"use strict";

// ─── Config ────────────────────────────────────────────────────────────────

const API_BASE  = "/api";
const PAGE_SIZE = 50;

// ─── State ─────────────────────────────────────────────────────────────────

const state = {
  // Find Jobs view
  jobs:       [],
  offset:     0,
  search:     "",
  source:     "",
  score:      "",
  isApplied:  "",   // "" | "true" | "false"
  loading:    false,
  scraping:   false,
  evaluating: false,
  activeJob:  null,

  // My Applications view
  view:         "jobs",   // "jobs" | "applications"
  applications: [],
  appsLoading:  false,
};

// ─── Application status metadata ───────────────────────────────────────────

const STATUS_META = {
  unapplied:   { label: "Unapplied",   css: "status-unapplied"   },
  applied:     { label: "Applied",     css: "status-applied"     },
  screening:   { label: "Screening",   css: "status-screening"   },
  interview:   { label: "Interview",   css: "status-interview"   },
  offer:       { label: "Offer",       css: "status-offer"       },
  rejection:   { label: "Rejection",   css: "status-rejection"   },
  withdrawn:   { label: "Withdrawn",   css: "status-withdrawn"   },
  "no response": { label: "No Response", css: "status-no-response" },
};

const STATUS_ORDER = [
  "applied", "screening", "interview", "offer",
  "rejection", "withdrawn", "no response",
];

// ─── DOM refs – existing ────────────────────────────────────────────────────

const $grid          = document.getElementById("jobGrid");
const $searchInput   = document.getElementById("searchInput");
const $sourceFilter  = document.getElementById("sourceFilter");
const $scoreFilter   = document.getElementById("scoreFilter");
const $appliedFilter = document.getElementById("appliedFilter");
const $refreshBtn    = document.getElementById("refreshBtn");
const $evalBtn       = document.getElementById("evalBtn");
const $loadMoreBtn   = document.getElementById("loadMoreBtn");
const $loadMoreWrap  = document.getElementById("loadMoreWrap");
const $emptyState    = document.getElementById("emptyState");
const $resultsInfo   = document.getElementById("resultsInfo");
const $totalCount    = document.getElementById("totalCount");
const $evalCount     = document.getElementById("evalCount");
const $statusBanner  = document.getElementById("statusBanner");

// Modal refs
const $modal              = document.getElementById("jobModal");
const $modalClose         = document.getElementById("modalClose");
const $modalTitle         = document.getElementById("modalTitle");
const $modalCompany       = document.getElementById("modalCompany");
const $modalSource        = document.getElementById("modalSource");
const $modalScore         = document.getElementById("modalScore");
const $modalVerified      = document.getElementById("modalVerified");
const $modalLoc           = document.getElementById("modalLocation");
const $modalDate          = document.getElementById("modalDate");
const $modalSalary        = document.getElementById("modalSalary");
const $modalDesc          = document.getElementById("modalDescription");
const $modalReasoning     = document.getElementById("modalReasoning");
const $modalReasoningText = document.getElementById("modalReasoningText");
const $modalVerification  = document.getElementById("modalVerification");
const $modalVerifText     = document.getElementById("modalVerificationText");
const $modalLink          = document.getElementById("modalLink");
const $modalApplyBtn      = document.getElementById("modalApplyBtn");

// ─── DOM refs – view tabs & applications ───────────────────────────────────

const $tabJobs           = document.getElementById("tabJobs");
const $tabApplications   = document.getElementById("tabApplications");
const $appliedBadge      = document.getElementById("appliedBadge");
const $jobsView          = document.getElementById("jobsView");
const $applicationsView  = document.getElementById("applicationsView");
const $applicationsGrid  = document.getElementById("applicationsGrid");
const $applicationsInfo  = document.getElementById("applicationsInfo");
const $applicationsSummary = document.getElementById("applicationsSummary");
const $applicationsEmpty = document.getElementById("applicationsEmpty");

// ─── Source display helpers ─────────────────────────────────────────────────

const SOURCE_META = {
  mycareersfuture: { label: "MyCareersFuture", css: "src-mycareersfuture" },
  jobstreet:       { label: "JobStreet",        css: "src-jobstreet"       },
  internsg:        { label: "InternSG",         css: "src-internsg"        },
  careers_gov:     { label: "Careers@Gov",      css: "src-careers_gov"     },
  external:        { label: "External",         css: "src-external"        },
};

function sourceMeta(source) {
  return SOURCE_META[source] || { label: source, css: "" };
}

// ─── Badge helpers ──────────────────────────────────────────────────────────

const SCORE_CSS = { High: "score-high", Medium: "score-medium", Low: "score-low" };

function scoreBadge(score) {
  if (!score) return "";
  return `<span class="suitability-badge ${SCORE_CSS[score] || ""}">${escapeHtml(score)}</span>`;
}

function verifiedBadge(company_verified) {
  if (company_verified === null || company_verified === undefined) return "";
  if (company_verified === true  || company_verified === 1)
    return `<span class="verified-badge verified-ok">Verified</span>`;
  return `<span class="verified-badge verified-flagged">&#9888; Flagged</span>`;
}

function statusBadge(appStatus) {
  const meta = STATUS_META[appStatus] || { label: appStatus, css: "" };
  return `<span class="app-status-badge ${meta.css}">${escapeHtml(meta.label)}</span>`;
}

// ─── Utility ────────────────────────────────────────────────────────────────

function escapeHtml(str = "") {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function formatDate(raw) {
  if (!raw) return "";
  try {
    const d = new Date(raw);
    if (isNaN(d)) return raw;
    return d.toLocaleDateString("en-SG", { day: "numeric", month: "short", year: "numeric" });
  } catch {
    return raw;
  }
}

/**
 * Format an ISO-8601 timestamp as "7 May 2026, 14:30" (UK-style, 24-hour).
 */
function formatAppliedAt(isoStr) {
  if (!isoStr) return null;
  try {
    const d = new Date(isoStr);
    if (isNaN(d)) return null;
    const date = d.toLocaleDateString("en-GB", {
      day: "numeric", month: "short", year: "numeric",
    });
    const time = d.toLocaleTimeString("en-GB", {
      hour: "2-digit", minute: "2-digit", hour12: false,
    });
    return `${date}, ${time}`;
  } catch {
    return null;
  }
}

function daysAgoLabel(days) {
  if (days === null || days === undefined) return null;
  if (days === 0) return "Today";
  if (days === 1) return "Yesterday";
  if (days <= 30) return `${days}d ago`;
  return null;
}

function snippet(text = "", maxLen = 120) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > maxLen ? clean.slice(0, maxLen) + "…" : clean;
}

let _debounceTimer;
function debounce(fn, ms = 300) {
  clearTimeout(_debounceTimer);
  _debounceTimer = setTimeout(fn, ms);
}

function showBanner(message, type = "info") {
  $statusBanner.className = `status-banner ${type}`;
  $statusBanner.textContent = message;
  $statusBanner.classList.remove("hidden");
  if (type !== "error") {
    setTimeout(() => $statusBanner.classList.add("hidden"), 4000);
  }
}

// ─── Skeleton loader ────────────────────────────────────────────────────────

function renderSkeletons(count = 6) {
  $grid.innerHTML = Array.from({ length: count }, () => `
    <div class="skeleton-card">
      <div class="skeleton-line short"></div>
      <div class="skeleton-line wide"  style="height:16px;margin-bottom:.75rem;"></div>
      <div class="skeleton-line med"></div>
      <div class="skeleton-line tall"></div>
      <div class="skeleton-line tall"></div>
    </div>
  `).join("");
}

// ─── Render job cards (Find Jobs view) ─────────────────────────────────────

function buildCard(job) {
  const meta = sourceMeta(job.source);
  const card = document.createElement("article");
  const appStatus = job.application_status || "unapplied";
  card.className = `job-card${appStatus !== "unapplied" ? " applied" : ""}`;
  card.dataset.id = job.id;

  const daysText = daysAgoLabel(job.days_since_posted);
  const dateDisplay = daysText || (job.date_posted ? formatDate(job.date_posted) : null);
  const showFullDate = daysText && job.date_posted;

  // Top-right badges: source + status + flagged
  const statusLabel = STATUS_META[appStatus]?.label || appStatus;
  const topBadges = [
    `<span class="source-badge ${meta.css}">${escapeHtml(meta.label)}</span>`,
    appStatus !== "unapplied"
      ? `<span class="app-status-badge ${STATUS_META[appStatus]?.css || ""}">${escapeHtml(statusLabel)}</span>`
      : "",
    job.company_verified === 0 || job.company_verified === false
      ? `<span class="verified-badge verified-flagged">&#9888; Flagged</span>` : "",
  ].filter(Boolean).join(" ");

  card.innerHTML = `
    <div class="card-top">
      <h3 class="card-title">${escapeHtml(job.title)}</h3>
      <div style="display:flex;gap:.35rem;align-items:center;flex-wrap:wrap">${topBadges}</div>
    </div>
    ${job.company ? `<p class="card-company">${escapeHtml(job.company)}</p>` : ""}
    <div class="card-meta">
      ${job.location ? `
        <span class="meta-chip">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"></path>
            <circle cx="12" cy="10" r="3"></circle>
          </svg>
          ${escapeHtml(job.location)}
        </span>` : ""}
      ${dateDisplay ? `
        <span class="meta-chip">
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none"
               stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <rect x="3" y="4" width="18" height="18" rx="2" ry="2"></rect>
            <line x1="16" y1="2" x2="16" y2="6"></line>
            <line x1="8" y1="2" x2="8" y2="6"></line>
            <line x1="3" y1="10" x2="21" y2="10"></line>
          </svg>
          ${escapeHtml(dateDisplay)}
          ${showFullDate ? `<span class="days-ago-note">${escapeHtml(formatDate(job.date_posted))}</span>` : ""}
        </span>` : ""}
    </div>
    ${job.salary ? `<p class="card-salary">${escapeHtml(job.salary)}</p>` : ""}
    ${job.description ? `<p class="card-snippet">${escapeHtml(snippet(job.description))}</p>` : ""}
    ${job.suitability_score ? `<div class="card-score-row">${scoreBadge(job.suitability_score)}</div>` : ""}
  `;

  card.addEventListener("click", () => openModal(job));
  return card;
}

function renderJobs(jobs, append = false) {
  if (!append) $grid.innerHTML = "";

  if (jobs.length === 0 && !append) {
    $emptyState.classList.remove("hidden");
    $loadMoreWrap.classList.add("hidden");
    return;
  }

  $emptyState.classList.add("hidden");
  const frag = document.createDocumentFragment();
  jobs.forEach(j => frag.appendChild(buildCard(j)));
  $grid.appendChild(frag);
}

// ─── Render application cards (My Applications view) ───────────────────────

/**
 * Build a status <select> dropdown that is styled according to the current value.
 * Changing the select triggers a PATCH call without closing any modal.
 */
function buildStatusSelect(job) {
  const appStatus = job.application_status || "unapplied";
  const meta      = STATUS_META[appStatus] || {};
  const select    = document.createElement("select");
  select.className = `status-select ${meta.css || ""}`;
  select.dataset.jobId = job.id;

  STATUS_ORDER.forEach(val => {
    const opt       = document.createElement("option");
    opt.value       = val;
    opt.textContent = STATUS_META[val]?.label || val;
    if (val === appStatus) opt.selected = true;
    select.appendChild(opt);
  });

  select.addEventListener("change", async (e) => {
    e.stopPropagation();   // prevent card click / modal open
    const newStatus = e.target.value;
    const result    = await updateJobStatus(job.id, newStatus, select);
    if (result) {
      // Update in-memory copy so a re-render reflects the change
      job.application_status = result.application_status;
      job.applied_at         = result.applied_at;
      job.is_applied         = result.is_applied;
      // Re-render the applied_at timestamp on this card
      const card     = $applicationsGrid.querySelector(`[data-id="${job.id}"]`);
      const stampEl  = card && card.querySelector(".applied-at-stamp");
      if (stampEl) {
        const formatted = formatAppliedAt(result.applied_at);
        stampEl.textContent = formatted ? `Applied on: ${formatted}` : "";
      }
    }
  });

  return select;
}

function buildApplicationCard(job) {
  const meta      = sourceMeta(job.source);
  const appStatus = job.application_status || "unapplied";
  const card      = document.createElement("article");
  card.className  = `job-card application-card`;
  card.dataset.id = job.id;

  const appliedStr  = formatAppliedAt(job.applied_at);
  const scoreHtml   = scoreBadge(job.suitability_score);

  // Header row: title + source badge
  const header = document.createElement("div");
  header.className = "card-top";
  header.innerHTML = `
    <h3 class="card-title">${escapeHtml(job.title)}</h3>
    <span class="source-badge ${meta.css}">${escapeHtml(meta.label)}</span>
  `;

  const company = document.createElement("p");
  company.className = "card-company";
  company.textContent = job.company || "";

  // Applied-at timestamp
  const stamp = document.createElement("p");
  stamp.className = "applied-at-stamp";
  stamp.textContent = appliedStr ? `Applied on: ${appliedStr}` : "";

  // Score (if available)
  const scoreRow = document.createElement("div");
  scoreRow.className = "card-score-row";
  scoreRow.innerHTML = scoreHtml;

  // Status selector row
  const statusRow = document.createElement("div");
  statusRow.className = "app-status-row";

  const statusLabel = document.createElement("span");
  statusLabel.className = "status-label";
  statusLabel.textContent = "Status:";

  statusRow.appendChild(statusLabel);
  statusRow.appendChild(buildStatusSelect(job));

  card.appendChild(header);
  if (job.company) card.appendChild(company);
  if (appliedStr)  card.appendChild(stamp);
  if (scoreHtml)   card.appendChild(scoreRow);
  card.appendChild(statusRow);

  // Open modal on card click (but NOT when interacting with the select)
  card.addEventListener("click", () => openModal(job));
  return card;
}

function renderStatusSummary(apps) {
  const counts = {};
  apps.forEach(j => {
    const s = j.application_status || "unapplied";
    if (s !== "unapplied") counts[s] = (counts[s] || 0) + 1;
  });

  const chips = STATUS_ORDER
    .filter(s => counts[s])
    .map(s => `<span class="summary-chip ${STATUS_META[s]?.css || ""}">${STATUS_META[s]?.label || s}: ${counts[s]}</span>`)
    .join("");

  if (chips) {
    $applicationsSummary.innerHTML = chips;
    $applicationsSummary.classList.remove("hidden");
  } else {
    $applicationsSummary.classList.add("hidden");
  }
}

function renderApplications(apps) {
  $applicationsGrid.innerHTML = "";

  if (apps.length === 0) {
    $applicationsEmpty.classList.remove("hidden");
    $applicationsSummary.classList.add("hidden");
    $applicationsInfo.textContent = "";
    return;
  }

  $applicationsEmpty.classList.add("hidden");
  renderStatusSummary(apps);
  $applicationsInfo.textContent =
    `${apps.length} application${apps.length !== 1 ? "s" : ""} tracked`;

  // Sort by applied_at descending (most recent first)
  const sorted = [...apps].sort((a, b) => {
    if (!a.applied_at && !b.applied_at) return 0;
    if (!a.applied_at) return 1;
    if (!b.applied_at) return -1;
    return new Date(b.applied_at) - new Date(a.applied_at);
  });

  const frag = document.createDocumentFragment();
  sorted.forEach(j => frag.appendChild(buildApplicationCard(j)));
  $applicationsGrid.appendChild(frag);
}

// ─── View switching ─────────────────────────────────────────────────────────

function switchView(view) {
  state.view = view;
  const toJobs = view === "jobs";

  $jobsView.classList.toggle("hidden", !toJobs);
  $applicationsView.classList.toggle("hidden", toJobs);
  $tabJobs.classList.toggle("active", toJobs);
  $tabJobs.setAttribute("aria-selected", toJobs ? "true" : "false");
  $tabApplications.classList.toggle("active", !toJobs);
  $tabApplications.setAttribute("aria-selected", toJobs ? "false" : "true");

  if (!toJobs) fetchApplications();
}

// ─── Fetch jobs (Find Jobs view) ────────────────────────────────────────────

async function fetchJobs({ append = false } = {}) {
  if (state.loading) return;
  state.loading = true;

  if (!append) {
    renderSkeletons();
    state.offset = 0;
    state.jobs   = [];
  }

  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: state.offset });
  if (state.search)    params.set("search",     state.search);
  if (state.source)    params.set("source",     state.source);
  if (state.score)     params.set("score",      state.score);
  if (state.isApplied) params.set("is_applied", state.isApplied);

  try {
    const res  = await fetch(`${API_BASE}/jobs?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    const newJobs = data.jobs || [];
    state.jobs   = append ? [...state.jobs, ...newJobs] : newJobs;
    state.offset += newJobs.length;

    renderJobs(state.jobs, false);
    $loadMoreWrap.classList.toggle("hidden", newJobs.length < PAGE_SIZE);
    $resultsInfo.textContent =
      `Showing ${state.jobs.length} internship${state.jobs.length !== 1 ? "s" : ""}`;

  } catch (err) {
    console.error("fetchJobs error:", err);
    $grid.innerHTML = "";
    showBanner(`Failed to load jobs: ${err.message}`, "error");
  } finally {
    state.loading = false;
  }
}

// ─── Fetch applications (My Applications view) ─────────────────────────────

async function fetchApplications() {
  if (state.appsLoading) return;
  state.appsLoading = true;
  $applicationsGrid.innerHTML = `
    <div class="skeleton-card"><div class="skeleton-line short"></div>
    <div class="skeleton-line wide" style="height:16px"></div>
    <div class="skeleton-line med"></div></div>
  `.repeat(3);

  try {
    const res  = await fetch(`${API_BASE}/jobs?app_status=active&limit=200`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    state.applications = data.jobs || [];
    renderApplications(state.applications);
  } catch (err) {
    console.error("fetchApplications error:", err);
    $applicationsGrid.innerHTML = "";
    showBanner(`Failed to load applications: ${err.message}`, "error");
  } finally {
    state.appsLoading = false;
  }
}

// ─── Fetch stats ────────────────────────────────────────────────────────────

async function fetchStats() {
  try {
    const res  = await fetch(`${API_BASE}/stats`);
    if (!res.ok) return;
    const data = await res.json();
    $totalCount.textContent = `${data.total.toLocaleString()} internships`;

    const scored = Object.values(data.eval_scores || {}).reduce((a, b) => a + b, 0);
    if (scored > 0) {
      $evalCount.textContent   = `${scored} scored`;
      $evalCount.style.display = "";
    }

    // Update the tab badge with total active applications
    const applied = data.applied_count || 0;
    if (applied > 0) {
      $appliedBadge.textContent    = applied;
      $appliedBadge.style.display  = "";
    } else {
      $appliedBadge.style.display  = "none";
    }
  } catch { /* non-critical */ }
}

// ─── Trigger scrape ─────────────────────────────────────────────────────────

async function triggerScrape() {
  if (state.scraping) return;
  state.scraping = true;
  $refreshBtn.disabled = true;
  showBanner("Scrape started — this may take a few minutes…", "info");

  try {
    const res  = await fetch(`${API_BASE}/scrape`, { method: "POST" });
    const data = await res.json();
    showBanner(data.message || "Scrape triggered.", "success");
    let polls = 0;
    const poll = setInterval(async () => {
      await fetchStats();
      if (++polls >= 12) { clearInterval(poll); await fetchJobs(); }
    }, 10_000);
  } catch (err) {
    showBanner(`Could not trigger scrape: ${err.message}`, "error");
  } finally {
    state.scraping = false;
    $refreshBtn.disabled = false;
  }
}

// ─── Trigger evaluation ─────────────────────────────────────────────────────

async function triggerEvaluate() {
  if (state.evaluating) return;
  state.evaluating = true;
  $evalBtn.disabled = true;
  showBanner("LLM evaluation started — scoring unscored jobs in background…", "info");

  try {
    const res  = await fetch(`${API_BASE}/evaluate`, { method: "POST" });
    const data = await res.json();
    showBanner(data.message || "Evaluation triggered.", "success");
    let polls = 0;
    const poll = setInterval(async () => {
      polls++;
      await fetchStats();
      try {
        const sr = await fetch(`${API_BASE}/eval-status`);
        const sd = await sr.json();
        if (!sd.is_running || polls >= 20) { clearInterval(poll); await fetchJobs(); }
      } catch { clearInterval(poll); }
    }, 15_000);
  } catch (err) {
    showBanner(`Could not trigger evaluation: ${err.message}`, "error");
  } finally {
    state.evaluating = false;
    $evalBtn.disabled = false;
  }
}

// ─── Update application status ──────────────────────────────────────────────

/**
 * PATCH /api/jobs/{id}/status with the new status value.
 * Updates the select element's CSS class to reflect the new colour immediately.
 *
 * @param {number}          jobId    - DB job ID
 * @param {string}          status   - One of the VALID_STATUSES values
 * @param {HTMLSelectElement} selectEl - The select to update on success (optional)
 * @returns {object|null} The API response payload, or null on error.
 */
async function updateJobStatus(jobId, status, selectEl = null) {
  try {
    const res = await fetch(`${API_BASE}/jobs/${jobId}/status`, {
      method:  "PATCH",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ status }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Update the select's colour class immediately
    if (selectEl) {
      const meta = STATUS_META[status] || {};
      selectEl.className = `status-select ${meta.css || ""}`;
    }

    showBanner(`Status updated to "${STATUS_META[status]?.label || status}".`, "success");

    // Refresh the tab badge count
    await fetchStats();
    return data;
  } catch (err) {
    showBanner(`Could not update status: ${err.message}`, "error");
    // Revert the select to its previous value
    if (selectEl) {
      const jobInMem = state.applications.find(j => j.id === jobId);
      if (jobInMem) selectEl.value = jobInMem.application_status || "applied";
    }
    return null;
  }
}

// ─── Toggle applied (modal button) ──────────────────────────────────────────

async function toggleApply(job) {
  const current   = job.application_status || "unapplied";
  const newStatus = current === "unapplied" ? "applied" : "unapplied";

  try {
    const res  = await fetch(`${API_BASE}/jobs/${job.id}/apply`, { method: "PATCH" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();

    // Update in-memory copy
    job.application_status = data.application_status;
    job.applied_at         = data.applied_at;
    job.is_applied         = data.is_applied;

    // Update the open modal button
    _syncApplyButton(job);

    // Re-render the card in the Find Jobs grid
    const card = $grid.querySelector(`[data-id="${job.id}"]`);
    if (card) card.replaceWith(buildCard(job));

    showBanner(
      data.is_applied ? "Added to My Applications." : "Removed from My Applications.",
      "success",
    );

    // If the applications view is open, refresh it
    if (state.view === "applications") fetchApplications();
    await fetchStats();
  } catch (err) {
    showBanner(`Could not update applied state: ${err.message}`, "error");
  }
}

function _syncApplyButton(job) {
  if (!$modalApplyBtn) return;
  const isTracked = job && job.application_status && job.application_status !== "unapplied";
  $modalApplyBtn.textContent = isTracked ? "Applied ✓" : "Mark as Applied";
  $modalApplyBtn.classList.toggle("applied", isTracked);
}

// ─── Modal ──────────────────────────────────────────────────────────────────

function openModal(job) {
  state.activeJob = job;

  const meta = sourceMeta(job.source);
  $modalSource.textContent = meta.label;
  $modalSource.className   = `modal-source-badge ${meta.css}`;
  $modalTitle.textContent  = job.title   || "";
  $modalCompany.textContent = job.company || "";

  $modalLoc.textContent = job.location || "";

  const daysText = daysAgoLabel(job.days_since_posted);
  if (daysText) {
    $modalDate.textContent = `${daysText}${job.date_posted ? " · " + formatDate(job.date_posted) : ""}`;
  } else {
    $modalDate.textContent = formatDate(job.date_posted);
  }

  $modalSalary.textContent = job.salary || "";
  $modalDesc.textContent   = job.description || "No description available.";
  $modalLink.href          = job.url || "#";

  $modalScore.innerHTML    = scoreBadge(job.suitability_score);
  $modalScore.style.display = job.suitability_score ? "" : "none";

  if (job.source === "external" && job.company_verified !== null && job.company_verified !== undefined) {
    $modalVerified.innerHTML    = verifiedBadge(job.company_verified);
    $modalVerified.style.display = "";
  } else {
    $modalVerified.innerHTML    = "";
    $modalVerified.style.display = "none";
  }

  if (job.llm_reasoning) {
    $modalReasoningText.textContent = job.llm_reasoning;
    $modalReasoning.classList.remove("hidden");
    $modalReasoning.open = false;
  } else {
    $modalReasoning.classList.add("hidden");
  }

  if (job.verification_notes) {
    $modalVerifText.textContent = job.verification_notes;
    $modalVerification.classList.remove("hidden");
    $modalVerification.open = false;
  } else {
    $modalVerification.classList.add("hidden");
  }

  _syncApplyButton(job);

  $modal.classList.remove("hidden");
  document.body.style.overflow = "hidden";
}

function closeModal() {
  $modal.classList.add("hidden");
  document.body.style.overflow = "";
  state.activeJob = null;
}

// ─── Event listeners ────────────────────────────────────────────────────────

$searchInput.addEventListener("input", () => {
  debounce(() => { state.search = $searchInput.value.trim(); fetchJobs(); }, 350);
});

$sourceFilter.addEventListener("change", () => {
  state.source = $sourceFilter.value;
  fetchJobs();
});

$scoreFilter.addEventListener("change", () => {
  state.score = $scoreFilter.value;
  fetchJobs();
});

$appliedFilter.addEventListener("change", () => {
  state.isApplied = $appliedFilter.value;
  fetchJobs();
});

$evalBtn.addEventListener("click", triggerEvaluate);
$refreshBtn.addEventListener("click", triggerScrape);

$loadMoreBtn.addEventListener("click", () => fetchJobs({ append: true }));

$modalClose.addEventListener("click", closeModal);
$modal.addEventListener("click", (e) => { if (e.target === $modal) closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

$modalApplyBtn.addEventListener("click", () => {
  if (state.activeJob) toggleApply(state.activeJob);
});

// View tab listeners
$tabJobs.addEventListener("click", () => switchView("jobs"));
$tabApplications.addEventListener("click", () => switchView("applications"));

// ─── Init ───────────────────────────────────────────────────────────────────

(async () => {
  await Promise.all([fetchStats(), fetchJobs()]);
})();
