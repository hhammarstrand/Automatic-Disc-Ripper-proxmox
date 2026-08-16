/**
 * Automatic Disc Ripper – Dashboard auto-refresh and utility functions.
 */

// ------------------------------------------------------------------ //
// Saying things, and asking things
//
// The browser's alert() and confirm() were used sixty-odd times here. They
// block the page, they look nothing like it, and on a phone they are a
// full-screen system dialog for "3 jobs removed". These are the replacements
// and every call site uses them.
//
// notify() is for something that has happened: it appears, it does not stop
// anyone doing the next thing, and it goes away. Anything that went wrong
// stays until it is dismissed — a message you did not read is the same as no
// message, and the ones that matter are exactly the ones nobody wants to
// read.
//
// confirmAction() is for something about to happen. It returns a promise
// rather than a boolean, which is the one place these are not a drop-in
// replacement — and worth it, because it can then show the *detail* of what
// is about to happen instead of cramming a list of file paths into a string.
// ------------------------------------------------------------------ //

// The reason a request failed, whichever key it arrived under.
//
// The server now sends both `error` and `message` on every failure, so this
// is a belt to that braces — but it is also the one place to change if a
// route ever forgets, instead of twenty-three copies of `data.error ||
// 'Unknown error'` scattered across the front-end, each of which reads only
// the key its author happened to know about.
// A poster, or a tile that admits there is not one.
//
// TMDb has no poster for a great many of the things people actually own —
// regional children's television especially — so the missing case is not an
// edge case here, it is most of a Swedish library. A row that simply loses
// its image column when the poster is absent makes the list ragged and the
// titles stop lining up, which is worse than a plain grey tile.
//
// Built as elements rather than markup for the same reason the show list is:
// a title with an apostrophe in it broke every handler on the page the last
// time this was a template string.
function posterPlaceholder(kind) {
    const tile = document.createElement('div');
    tile.className = 'adr-poster adr-poster-none';
    const icon = document.createElement('i');
    icon.className = kind === 'movie' ? 'bi bi-film' : 'bi bi-collection-play';
    tile.appendChild(icon);
    return tile;
}

function posterThumb(url, kind = 'tv') {
    if (!url) return posterPlaceholder(kind);
    const img = document.createElement('img');
    img.className = 'adr-poster';
    img.src = url;
    img.alt = '';                    // decorative: the title is right beside it
    img.loading = 'lazy';
    // The dimensions are in the attributes as well as the stylesheet so the
    // row reserves its space before the image arrives — over a phone's wifi
    // the list otherwise reflows under the thumb as each poster lands.
    img.width = 46;
    img.height = 69;
    // A dead or blocked URL renders as a broken-image glyph otherwise. The
    // tile is what "no poster" looks like everywhere else in this list.
    img.addEventListener('error', () => img.replaceWith(posterPlaceholder(kind)),
                         {once: true});
    return img;
}

function reasonFrom(payload, fallback = 'the server did not say why') {
    if (!payload) return fallback;
    return payload.error || payload.message || fallback;
}

// Do this once the typing stops.
//
// A search that fires on every keystroke sends one request per letter, and
// "The Wire" is eight of them against a rate-limited third-party API — seven
// of which are answers nobody will ever read. Waiting for a pause is what
// makes searching-as-you-type affordable at all.
//
// The delay is a judgement, not a constant to tune: shorter and a normal
// typing rhythm still fires mid-word; longer and it feels like the field has
// stopped responding.
function debounce(fn, ms) {
    let timer = null;
    return function (...args) {
        clearTimeout(timer);
        timer = setTimeout(() => fn.apply(this, args), ms);
    };
}

const SEARCH_DEBOUNCE_MILLISECONDS = 400;

// Times, in the zone of whoever is reading them.
//
// The server renders each timestamp too, so the page is readable with no
// JavaScript at all — this rewrites it. Worth doing even once the container's
// clock is right, because the container's clock and the reader's are not
// necessarily the same one: a phone on holiday should still show the time the
// disc actually finished, in the zone the person holding it is standing in.
//
// The offset comes from the server (see the isotime filter). Without it a
// browser reads a zoneless date-time as its *own* local time, which is how a
// job that had just started showed an elapsed time of two hours.
function formatLocalTimes(root = document) {
    root.querySelectorAll('time[data-iso]').forEach(element => {
        const iso = element.getAttribute('data-iso');
        if (!iso) return;
        const when = new Date(iso);
        if (isNaN(when)) return;              // leave the server's rendering
        const short = element.getAttribute('data-format') === 'short';
        element.textContent = when.toLocaleString(undefined, short ? {
            day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
        } : {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit',
        });
        element.title = when.toLocaleString();
    });
}

document.addEventListener('DOMContentLoaded', () => formatLocalTimes());

const TOAST_MILLISECONDS = 5000;

function notify(message, kind = 'info') {
    const host = document.getElementById('toastHost');
    if (!host) { console.log(message); return; }   // a page without base.html

    const icons = {
        success: 'bi-check-circle-fill',
        danger: 'bi-exclamation-octagon-fill',
        warning: 'bi-exclamation-triangle-fill',
        info: 'bi-info-circle-fill',
    };
    const element = document.createElement('div');
    element.className = `toast align-items-center border-0 adr-toast adr-toast-${kind}`;
    element.setAttribute('role', 'alert');
    element.innerHTML = `
        <div class="d-flex">
            <div class="toast-body">
                <i class="bi ${icons[kind] || icons.info} me-2"></i>${escapeHtml(message)}
            </div>
            <button type="button" class="btn-close btn-close-white me-2 m-auto"
                    data-bs-dismiss="toast" aria-label="Close"></button>
        </div>`;
    host.appendChild(element);

    // Problems stay put. Everything else has said its piece in five seconds.
    const toast = new bootstrap.Toast(element, {
        autohide: kind !== 'danger' && kind !== 'warning',
        delay: TOAST_MILLISECONDS,
    });
    element.addEventListener('hidden.bs.toast', () => element.remove());
    toast.show();
}

function confirmAction({title, body, detail = '', confirmLabel = 'Confirm', danger = false}) {
    return new Promise(resolve => {
        const element = document.getElementById('confirmModal');
        if (!element) { resolve(window.confirm(body)); return; }

        document.getElementById('confirmTitle').textContent = title;
        document.getElementById('confirmBody').innerHTML = body;

        // The detail pane exists so a delete can list the actual paths. A
        // count is not something anyone can check; a list is.
        const detailPane = document.getElementById('confirmDetail');
        detailPane.textContent = detail;
        detailPane.classList.toggle('d-none', !detail);

        const go = document.getElementById('confirmGo');
        go.textContent = confirmLabel;
        go.className = 'btn ' + (danger ? 'btn-danger' : 'btn-primary');

        const modal = bootstrap.Modal.getOrCreateInstance(element);
        let answer = false;
        const accept = () => { answer = true; modal.hide(); };
        go.addEventListener('click', accept, {once: true});
        element.addEventListener('hidden.bs.modal', () => {
            go.removeEventListener('click', accept);
            resolve(answer);
        }, {once: true});
        modal.show();
    });
}

// ------------------------------------------------------------------ //
// Auto-refresh active jobs every 5 seconds
// ------------------------------------------------------------------ //

const REFRESH_INTERVAL = 5000;


// ------------------------------------------------------------------ //
// Polling that cannot stack, and a badge that tells the truth
//
// Every polling loop here used a bare setInterval: when the server is slow —
// mid-encode on a small CPU is exactly when — the next tick fires before the
// last answer arrives, requests pile up, and the answers can come back out of
// order so an older state overwrites a newer one. Each loop now skips its
// tick while one is in flight.
//
// The badge in the navbar was the string "Online", hardcoded in the template,
// shown identically whether the last poll answered or the service had been
// down for an hour. It now reflects the last poll's outcome, which is the
// entire job of a badge that says Online.
// ------------------------------------------------------------------ //
const _inflight = new Set();

// ------------------------------------------------------------------ //
// Reloading the page without throwing away what someone is doing
//
// Several of the pollers reload the whole page when the server's answer no
// longer matches what is on screen — a new job appeared, a job changed phase,
// the preflight warning became wrong. That is the right thing to do with a
// page nobody is touching, and the wrong thing to do at the exact moment it
// happens most: you are standing at the drives, naming disc 1 in the series
// dialog, and inserting disc 2 is what creates the new job. The reload took
// the half-typed show name with it.
//
// So a reload waits for the user to be finished rather than being cancelled —
// a stale dashboard is its own problem. Busy means a modal or sheet is open,
// or the focus is in a field; the check is repeated at fire time rather than
// remembered, because "busy" is a state that ends without an event we can
// listen for.
// ------------------------------------------------------------------ //

let _reloadPending = false;

function uiIsBusy() {
    return document.body.classList.contains('modal-open')
        || document.querySelector('.offcanvas.show') !== null
        || (document.activeElement !== null
            && typeof document.activeElement.matches === 'function'
            && document.activeElement.matches('input, textarea, select'));
}

function safeReload() {
    if (!uiIsBusy()) { location.reload(); return; }
    // One pending reload, however many pollers asked for one: five seconds of
    // a busy dialog would otherwise leave five timers all racing to reload.
    if (_reloadPending) return;
    _reloadPending = true;
    const timer = setInterval(() => {
        if (uiIsBusy()) return;
        clearInterval(timer);
        // Cleared before the navigation, not after: a reload a browser
        // declines to perform — an unload handler, a page already leaving —
        // would otherwise leave the flag set and every later reload request
        // silently dropped for the life of the page.
        _reloadPending = false;
        location.reload();
    }, 1000);
}

function pollWithoutStacking(name, work) {
    if (_inflight.has(name)) return Promise.resolve();
    _inflight.add(name);
    return Promise.resolve()
        .then(work)
        .then(() => setConnectionState(true))
        .catch(() => setConnectionState(false))
        .finally(() => _inflight.delete(name));
}

function setConnectionState(up) {
    const badge = document.getElementById('connBadge');
    if (!badge) return;
    badge.className = up ? 'badge bg-success' : 'badge bg-danger';
    badge.textContent = up ? 'Online' : 'No answer';
    badge.title = up ? '' : 'The last status request got no reply — the service may be restarting.';
}

function refreshDashboard() {
    // Only refresh on the dashboard page
    if (window.location.pathname !== '/') return Promise.resolve();

    return fetch('/api/jobs/active')
        .then(r => r.json())
        .then(activeJobs => {

            // Detect new jobs that don't have a card yet → full reload
            const displayedIds = new Set(
                Array.from(document.querySelectorAll('[data-job-id]'))
                    .map(el => parseInt(el.dataset.jobId))
            );
            const hasNewJob = activeJobs.some(j => !displayedIds.has(j.id));
            if (hasNewJob) {
                safeReload();
                return;
            }

            updateActiveJobs(activeJobs);
            updateQueueSize();
            checkPreflight();
        })
        .catch(err => {
            // Rethrown, not swallowed. pollWithoutStacking decides the
            // connection badge from whether this rejects, and catching it
            // here made setConnectionState(false) unreachable: the badge
            // said "Online" permanently, including while the service was
            // down — the one moment it exists to report.
            console.warn('Refresh failed:', err);
            throw err;
        });
}

function updateActiveJobs(jobs) {
    jobs.forEach(job => {
        const card = document.querySelector(`[data-job-id="${job.id}"]`);
        if (!card) return; // New job appeared – full page reload needed

        // If status changed (e.g. ripped→encoding), reload to get correct layout
        const prevStatus = card.dataset.jobStatus;
        if (prevStatus && prevStatus !== job.status) {
            safeReload();
            return;
        }
        card.dataset.jobStatus = job.status;

        // Update progress bar (not present for queued/ripped jobs)
        const bar = card.querySelector('.progress-bar');
        if (bar) {
            // phase_progress can be 0.0 which is falsy — use explicit null check
            const rawPct = (job.phase_progress != null ? job.phase_progress : job.progress);
            if (typeof rawPct !== 'number' || isNaN(rawPct)) {
                console.warn('ADR: non-numeric phase_progress for job', job.id, rawPct);
            }
            // Whole percent. The gauge is read from across the room, and a
            // tenth of a percent on a forty-minute encode is a digit that
            // changes every few seconds and says nothing.
            const pct = Math.round((rawPct || 0) * 100);
            bar.style.width = pct + '%';
            bar.setAttribute('aria-valuenow', pct);

            // Update bar colour based on status
            bar.className = 'progress-bar';
            if (job.status === 'ripping') bar.classList.add('bg-warning');
            else if (job.status === 'encoding') bar.classList.add('bg-info');
            else bar.classList.add('bg-primary');

            // The counters. The percentage is no longer inside the bar: it is
            // an element of its own, big enough to read, and measurable by the
            // contrast audit, which cannot see text in a pseudo-element or
            // judge it inside a clipped flex box.
            const pctEl = card.querySelector(`[data-job-pct="${job.id}"]`);
            if (pctEl) {
                pctEl.replaceChildren(document.createTextNode(String(pct)));
                const unit = document.createElement('span');
                unit.className = 'adr-unit';
                unit.textContent = '%';
                pctEl.appendChild(unit);
            }
            const leftEl = card.querySelector(`[data-job-remaining="${job.id}"]`);
            if (leftEl) {
                const eta = job.progress_info && job.progress_info.eta_seconds;
                leftEl.textContent = eta > 0 ? '~' + formatRemaining(eta) : '—';
            }
        }

        // Update title (in case of re-match during rip/encode)
        const titleEl = card.querySelector('h6');
        if (titleEl && job.display_title) {
            titleEl.textContent = job.display_title;
        }

        // Update rich progress detail line
        const detailEl = card.querySelector(`[data-job-detail="${job.id}"]`);
        if (detailEl) {
            detailEl.textContent = formatProgressDetail(job);
        }

        // And what the tool itself is saying, when that is not already the
        // whole of the line above.
        const sayingEl = card.querySelector(`[data-job-saying="${job.id}"]`);
        if (sayingEl) {
            const said = (job.progress_info && job.progress_info.description) || '';
            sayingEl.textContent = said === detailEl?.textContent ? '' : said;
        }
    });

    // If a job finished (no longer in active list), reload the page
    const displayedIds = Array.from(document.querySelectorAll('[data-job-id]'))
        .map(el => parseInt(el.dataset.jobId));
    const activeIds = jobs.map(j => j.id);
    const finishedAny = displayedIds.some(id => !activeIds.includes(id));
    if (finishedAny) {
        setTimeout(safeReload, 500);
    }
}

/**
 * Reload when the "ripping will fail" banner becomes wrong.
 *
 * The banner is rendered by the server, which keeps one description of the
 * problem instead of two. So rather than re-render it here, this only watches
 * for the answer changing — you fix the NAS, and the warning goes away without
 * you having to wonder whether the page is stale. The endpoint is cached
 * server-side, so polling it costs nothing.
 */
function checkPreflight() {
    const shownAsBlocked = document.getElementById('preflightBanner') !== null;
    fetch('/api/preflight')
        .then(r => r.json())
        .then(d => {
            if (d.ok === shownAsBlocked) safeReload();
        })
        .catch(() => {});
}

function updateQueueSize() {
    fetch('/api/status')
        .then(r => r.json())
        .then(data => {
            const el = document.getElementById('queueSize');
            if (el) el.textContent = data.encode_queue_size || 0;
        })
        .catch(() => {});
}

// ------------------------------------------------------------------ //
// Job actions
// ------------------------------------------------------------------ //

function cancelJob(jobId) {
    confirmAction({
        title: "Cancel this job?",
        body: 'Are you sure you want to cancel job #' + jobId + '?',
        confirmLabel: "Cancel the job",
        danger: true,
    }).then(confirmed => {
        if (!confirmed) return;

        fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    location.reload();
                } else {
                    notify('Could not cancel: ' + (reasonFrom(data)), 'danger');
                }
            })
            .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

function toggleDrive(driveLetter, disable, shown) {
    const action = disable ? 'disable' : 'enable';
    confirmAction({
        title: "Change this drive?",
        body: `Do you want to ${action} drive ${escapeHtml(shown || driveLetter)}?`,
        confirmLabel: "Yes, do it",
        danger: false,
    }).then(confirmed => {
        if (!confirmed) return;

        fetch('/api/drives/toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device: driveLetter, disabled: disable }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.ok) location.reload();
                else notify('Could not change: ' + (reasonFrom(data)), 'danger');
            })
            .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

// ------------------------------------------------------------------ //
// Rip the disc that is already loaded
//
// Insertion is an event; a disc sitting in a drive is a state. The watcher
// only sees the former, so after a failure nothing re-triggers however long
// you wait. Ejecting and reinserting works, but walking to the machine to
// restart software is not a fix.
// ------------------------------------------------------------------ //

function ripNow(driveLetter) {
    fetch('/api/drives/rip', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device: driveLetter }),
    })
        .then(r => r.json().then(d => ({ ok: r.ok, d })))
        .then(({ ok, d }) => {
            if (!ok) { notify(reasonFrom(d, 'Could not start the rip.'), 'danger'); return; }
            setTimeout(() => location.reload(), 800);
        })
        .catch(err => notify('Error: ' + err.message, 'danger'));
}

// ------------------------------------------------------------------ //
// Drive eject toggle
// ------------------------------------------------------------------ //

function toggleEject(driveLetter, enable, shown) {
    const label = enable ? 'enable auto-eject' : 'disable auto-eject';
    confirmAction({
        title: "Change this drive?",
        body: `Do you want to ${label} for drive ${escapeHtml(shown || driveLetter)}?`,
        confirmLabel: "Yes, do it",
        danger: false,
    }).then(confirmed => {
        if (!confirmed) return;

        fetch('/api/drives/eject-toggle', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ device: driveLetter, auto_eject: enable }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.ok) location.reload();
                else notify('Could not change eject setting: ' + (reasonFrom(data)), 'danger');
            })
            .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

// ------------------------------------------------------------------ //
// Drive eject (open tray)
// ------------------------------------------------------------------ //

function ejectDrive(driveLetter) {
    fetch('/api/drives/eject', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device: driveLetter }),
    })
        .then(r => r.json())
        .then(data => {
            if (!data.ok) {
                notify('Could not eject: ' + (reasonFrom(data)), 'danger');
            }
        })
        .catch(err => notify('Error: ' + err.message, 'danger'));
}

// ------------------------------------------------------------------ //
// Drive label
// ------------------------------------------------------------------ //

function saveDriveLabel(driveLetter) {
    const input = document.getElementById('driveLabel_' + driveLetter.replace(':', ''));
    if (!input) return;
    const label = input.value.trim();

    fetch('/api/drives/label', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ device: driveLetter, label: label }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                location.reload();
            } else {
                notify('Could not save label: ' + (reasonFrom(data)), 'danger');
            }
        })
        .catch(err => notify('Error: ' + err.message, 'danger'));
}

// ------------------------------------------------------------------ //
// Clear history
// ------------------------------------------------------------------ //

function clearHistory() {
    confirmAction({
        title: "Clear the history?",
        body: 'Clear all completed, failed, and cancelled jobs from history?',
        confirmLabel: "Clear it",
        danger: true,
    }).then(confirmed => {
        if (!confirmed) return;

        fetch('/api/history/clear', { method: 'POST' })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    notify(`${data.deleted} jobs deleted.`, 'success');
                    location.reload();
                } else {
                    notify('Could not clear: ' + (reasonFrom(data)), 'danger');
                }
            })
            .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

function deleteJob(jobId) {
    confirmAction({
        title: "Delete this job?",
        body: 'Delete job #' + jobId + '?',
        confirmLabel: "Delete",
        danger: true,
    }).then(confirmed => {
        if (!confirmed) return;

        fetch(`/api/jobs/${jobId}`, { method: 'DELETE' })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    const row = document.querySelector(`tr[data-status] td:first-child`);
                    // Simple approach: reload page
                    location.reload();
                } else {
                    notify('Could not delete: ' + (reasonFrom(data)), 'danger');
                }
            })
            .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

// ------------------------------------------------------------------ //
// Plex move controls
// ------------------------------------------------------------------ //

function togglePlexMove(jobId, checked) {
    fetch(`/api/jobs/${jobId}/toggle-plex`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ move_to_plex: checked }),
    })
        .then(r => r.json())
        .then(data => {
            if (!data.ok) {
                notify('Could not change Plex flag: ' + (reasonFrom(data)), 'danger');
            }
        })
        .catch(err => notify('Error: ' + err.message, 'danger'));
}

function moveToPlexManual(jobId) {
    confirmAction({
        title: "Move to Plex?",
        body: 'Move files to the Plex folder?',
        confirmLabel: "Move",
        danger: false,
    }).then(confirmed => {
        if (!confirmed) return;
        fetch(`/api/jobs/${jobId}/move-to-plex`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
        })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    location.reload();
                } else {
                    notify('Could not move: ' + (reasonFrom(data)), 'danger');
                }
            })
            .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

// ------------------------------------------------------------------ //
// TMDb Re-match modal
// ------------------------------------------------------------------ //

function openRematchModal(jobId, currentTitle) {
    document.getElementById('rematchJobId').value = jobId;
    document.getElementById('rematchJobTitle').textContent = currentTitle;
    const field = document.getElementById('rematchQuery');
    field.value = currentTitle.replace(/\s*\(\d{4}\)\s*$/, '');
    document.getElementById('rematchYear').value = '';
    document.getElementById('rematchResults').innerHTML = '';
    document.getElementById('rematchEmpty').classList.add('d-none');
    const modal = new bootstrap.Modal(document.getElementById('rematchModal'));
    modal.show();
    // Inside the tap that opened it, for the keyboard, and selected rather
    // than merely focused: the field is prefilled with the title that matched
    // wrongly, so the first keystroke should replace it, not append to it.
    field.focus({preventScroll: true});
    field.select();
}

let _rematchSearchSeq = 0;

const _searchTmdbSoon = debounce(
    () => searchTmdb(true), SEARCH_DEBOUNCE_MILLISECONDS);

function onRematchInput() {
    _searchTmdbSoon();
}

function searchTmdb(fromTyping = false) {
    const query = document.getElementById('rematchQuery').value.trim();
    if (query.length < 2) {
        if (fromTyping) {
            document.getElementById('rematchResults').innerHTML = '';
            document.getElementById('rematchEmpty').classList.add('d-none');
        }
        return;
    }
    const year = document.getElementById('rematchYear').value.trim();
    const resultsDiv = document.getElementById('rematchResults');
    const spinner = document.getElementById('rematchSpinner');
    const empty = document.getElementById('rematchEmpty');

    resultsDiv.innerHTML = '';
    empty.classList.add('d-none');
    spinner.classList.remove('d-none');

    let url = `/api/tmdb/search?q=${encodeURIComponent(query)}`;
    if (year) url += `&year=${year}`;

    // Typing puts several of these in flight at once and they do not promise
    // to come back in order. Only the newest may paint — same guard as the
    // season preview's, for the same reason.
    const seq = ++_rematchSearchSeq;
    fetch(url)
        .then(r => r.json())
        .then(data => {
            if (seq !== _rematchSearchSeq) return;
            spinner.classList.add('d-none');
            if (data.error) {
                empty.textContent = data.error;
                empty.classList.remove('d-none');
                return;
            }
            const results = data.results || [];
            if (results.length === 0) {
                empty.textContent = 'No results found.';
                empty.classList.remove('d-none');
                return;
            }
            results.forEach(movie => {
                const col = document.createElement('div');
                // Full width on a phone. Two columns at 390px is a poster
                // beside three lines of 11px text, which is a card nobody can
                // read and a tap target the width of a thumb.
                col.className = 'col-12 col-sm-6 col-md-4';
                col.innerHTML = `
                    <div class="card h-100" style="cursor:pointer; background: #0d1117; border-color: var(--adr-border);"
                         onclick="applyRematch(${parseInt(movie.tmdb_id)})">
                        <div class="row g-0 h-100">
                            <div class="col-4">
                                ${movie.poster_url
                                    ? `<img src="${escapeHtml(movie.poster_url)}" class="img-fluid rounded-start" alt="" style="height:100%; object-fit:cover;">`
                                    : `<div class="d-flex align-items-center justify-content-center h-100 bg-secondary rounded-start"><i class="bi bi-film fs-3"></i></div>`
                                }
                            </div>
                            <div class="col-8">
                                <div class="card-body p-2">
                                    <h6 class="card-title mb-1 small">${escapeHtml(movie.title)}</h6>
                                    <p class="card-text mb-0"><small class="text-secondary">${escapeHtml(String(movie.year || '?'))}</small></p>
                                    <p class="card-text"><small class="text-secondary" style="font-size:.7rem;">${escapeHtml(movie.overview || '')}</small></p>
                                </div>
                            </div>
                        </div>
                    </div>`;
                resultsDiv.appendChild(col);
            });
        })
        .catch(err => {
            if (seq !== _rematchSearchSeq) return;
            spinner.classList.add('d-none');
            empty.textContent = 'Search error: ' + err.message;
            empty.classList.remove('d-none');
        });
}

function applyRematch(tmdbId) {
    const jobId = document.getElementById('rematchJobId').value;
    confirmAction({
        title: "Re-match this job?",
        body: 'Re-match this job to the selected movie?',
        confirmLabel: "Re-match",
        danger: false,
    }).then(confirmed => {
        if (!confirmed) return;

        fetch(`/api/jobs/${jobId}/rematch`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tmdb_id: tmdbId }),
        })
            .then(r => r.json())
            .then(data => {
                if (data.ok) {
                    bootstrap.Modal.getInstance(document.getElementById('rematchModal')).hide();
                    location.reload();
                } else {
                    notify('Could not re-match: ' + (reasonFrom(data)), 'danger');
                }
            })
            .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

// ------------------------------------------------------------------ //
// Helpers
// ------------------------------------------------------------------ //

function capitalize(str) {
    return str.charAt(0).toUpperCase() + str.slice(1);
}

/**
 * Format rich progress detail from job.progress_info.
 * Returns a human-readable string like:
 *   Ripping: "Title 2/5 · 67.3%"
 *   Encoding: "Title 1/3 · 45.2% · ~2m 15s · 155 fps"
 *   Scanning: "Scanning video..."
 *   Muxing: "Muxing..."
 */
function formatProgressDetail(job) {
    const pi = job.progress_info;
    if (!pi) return '';

    // One line, assembled from whichever of these the phase actually knows.
    // Ordered by how often it answers the question someone is asking: how
    // much longer, then how fast, then where in the disc, then how long it
    // has been going.
    const parts = [];

    if (pi.phase === 'ripping') {
        if (pi.title_total > 1) {
            parts.push(`Title ${pi.title_current}/${pi.title_total}`);
        }
        if (pi.title_progress != null && pi.title_total > 1) {
            parts.push((pi.title_progress * 100).toFixed(0) + '%');
        }
        pushPace(parts, pi);
        return parts.join(' \u00b7 ') || pi.description || '';
    }

    if (pi.phase === 'encoding') {
        if (pi.state === 'scanning') return 'Scanning video\u2026';
        if (pi.state === 'muxing') return 'Muxing MP4\u2026';
        if (pi.track_total > 1) {
            parts.push(`Title ${pi.track_current}/${pi.track_total}`);
        }
        if (pi.pass_total > 1) {
            parts.push(`Pass ${pi.pass_num + 1}/${pi.pass_total}`);
        }
        if (pi.fps > 0) parts.push(pi.fps.toFixed(0) + ' fps');
        return parts.join(' \u00b7 ');
    }

    if (pi.phase === 'imaging') {
        pushPace(parts, pi);
        return parts.length ? parts.join(' \u00b7 ') : (pi.description || '');
    }

    // Audio CDs and anything new: the module writes its own sentence.
    return pi.description || '';
}

/**
 * The three numbers that answer "how much longer, and is it healthy":
 * time remaining, read speed, time spent.
 *
 * Each is omitted when it is not known rather than shown as zero — during the
 * first seconds of a rip there is not enough history to estimate anything, and
 * a confident wrong number is worse than a blank.
 */
function pushPace(parts, pi) {
    // Neither time goes in here any more. The counters above this line are
    // the elapsed and the remaining, in figures large enough to read from
    // across the room; repeating both in 11px monospace underneath was the
    // same fact stated three times on one card. What is left is the pace,
    // which the counters cannot show.
    if (pi.bytes_per_second > 0) {
        parts.push(formatSpeed(pi.bytes_per_second));
    }
}

function formatSpeed(bytesPerSecond) {
    if (!bytesPerSecond || bytesPerSecond <= 0) return '';
    if (bytesPerSecond < 1024) return bytesPerSecond.toFixed(0) + ' B/s';
    const kb = bytesPerSecond / 1024;
    if (kb < 1024) return kb.toFixed(0) + ' KB/s';
    const mb = kb / 1024;
    return (mb >= 10 ? mb.toFixed(0) : mb.toFixed(1)) + ' MB/s';
}

function formatEta(seconds) {
    // Two units at most, like adr/progress.format_eta: "2h 13m 44s" implies a
    // precision an estimate does not have.
    if (seconds == null || seconds < 0) return '';
    if (seconds < 60) return `${Math.round(seconds)}s`;
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm ? `${h}h ${rm}m` : `${h}h`;
}

function formatRemaining(seconds) {
    // The counter's own wording, matching adr.progress.format_remaining
    // exactly: the server renders the first one and the browser every one
    // after it, and two spellings of the same duration would read as the
    // figure jumping on the first refresh.
    if (seconds == null || seconds < 0) return '';
    if (seconds < 60) return '<1m';
    const m = Math.floor(seconds / 60);
    if (m < 60) return `${m}m`;
    const h = Math.floor(m / 60);
    const rm = m % 60;
    return rm ? `${h}h ${rm}m` : `${h}h`;
}

function toggleDriveMore(button) {
    // Hide and Auto-eject live behind this on a phone. They are settings, not
    // things you press while standing at the drive, and four 44px controls do
    // not fit a 390px bay beside the drive's own name.
    const bay = button.closest('.card');
    if (!bay) return;
    const open = bay.classList.toggle('adr-open');
    button.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function escapeHtml(str) {
    const el = document.createElement('span');
    el.textContent = str || '';
    return el.innerHTML;
}


// Copying to the clipboard on a plain-http page.
//
// navigator.clipboard needs a secure context, and this application is served
// over http on a LAN address by design — so every copy button here did nothing
// at all, with no error, on the deployment the README describes. The diagnostics
// page had already learned this and guarded; the rest had not.
//
// The button is passed in rather than read off the global `event`, which is
// undefined in a module, in strict mode, and in any handler attached with
// addEventListener.
function copyToClipboard(text, btn, okLabel = '<i class="bi bi-check"></i>') {
    const restore = (original) => setTimeout(() => { btn.innerHTML = original; }, 1500);

    const confirmOnButton = () => {
        if (!btn) return;
        const original = btn.innerHTML;
        btn.innerHTML = okLabel;
        restore(original);
    };

    if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text)
            .then(confirmOnButton)
            .catch(() => fallbackCopy(text, confirmOnButton));
        return;
    }
    fallbackCopy(text, confirmOnButton);
}

// document.execCommand is deprecated and is the only thing that works without
// a secure context. When even that fails the text is put where it can be
// selected by hand, because a copy button that quietly does nothing is worse
// than one that says it cannot.
function fallbackCopy(text, onSuccess) {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    let copied = false;
    try {
        copied = document.execCommand('copy');
    } catch {
        copied = false;
    }
    document.body.removeChild(area);
    if (copied) {
        onSuccess();
    } else {
        notify('This browser will not copy from a plain-http page. '
            + 'Select the text and copy it by hand.', 'warning');
    }
}

function copyPath(path, btn) {
    copyToClipboard(path, btn);
}

// ------------------------------------------------------------------ //
// Video player
// ------------------------------------------------------------------ //

function openPlayer(jobId) {
    const video = document.getElementById('videoPlayer');
    const titleEl = document.getElementById('playerTitle');
    const fileList = document.getElementById('playerFileList');

    // Pause & reset any previous playback
    video.pause();
    video.removeAttribute('src');
    fileList.classList.add('d-none');
    fileList.innerHTML = '';
    titleEl.textContent = 'Loading...';

    const modal = new bootstrap.Modal(document.getElementById('playerModal'));
    modal.show();

    fetch(`/api/jobs/${jobId}/files`)
        .then(r => r.json())
        .then(data => {
            if (data.error) {
                titleEl.textContent = 'Error: ' + data.error;
                return;
            }
            const files = data.files || [];
            titleEl.textContent = data.title || 'Video Player';

            if (files.length === 0) {
                titleEl.textContent += ' — No MP4 files found';
                return;
            }

            // If multiple files, show a file picker
            if (files.length > 1) {
                fileList.classList.remove('d-none');
                files.forEach((f, i) => {
                    const btn = document.createElement('button');
                    btn.className = 'btn btn-sm me-1 mb-1 ' + (i === 0 ? 'btn-success' : 'btn-outline-light');
                    btn.textContent = `${f.name} (${f.size_mb} MB)`;
                    btn.onclick = () => {
                        playFile(jobId, f.name);
                        fileList.querySelectorAll('button').forEach(b => b.className = 'btn btn-sm me-1 mb-1 btn-outline-light');
                        btn.className = 'btn btn-sm me-1 mb-1 btn-success';
                    };
                    fileList.appendChild(btn);
                });
            }

            // Auto-play the first file
            playFile(jobId, files[0].name);
        })
        .catch(err => {
            titleEl.textContent = 'Error: ' + err.message;
        });
}

function playFile(jobId, filename) {
    const video = document.getElementById('videoPlayer');
    // Each segment encoded, the separators left alone: an extra is
    // "Other/Extra 1.mkv", and encoding the slash to %2F gives a path many
    // servers reject outright rather than a subfolder.
    const encodedPath = String(filename).split('/').map(encodeURIComponent).join('/');
    video.src = `/api/jobs/${jobId}/stream/${encodedPath}`;
    video.load();
    video.play().catch(() => {}); // autoplay may be blocked
}

// Stop video when modal closes
document.addEventListener('DOMContentLoaded', () => {
    const playerModal = document.getElementById('playerModal');
    if (playerModal) {
        playerModal.addEventListener('hidden.bs.modal', () => {
            const video = document.getElementById('videoPlayer');
            video.pause();
            video.removeAttribute('src');
            video.load();
        });
    }
});

// ------------------------------------------------------------------ //
// Error detail modal
// ------------------------------------------------------------------ //

function showError(jobId, title, errorText) {
    document.getElementById('errorModalJob').textContent = '#' + jobId;
    document.getElementById('errorModalTitle').textContent = title;
    document.getElementById('errorModalText').textContent =
        errorText || 'No error recorded — this job did not fail.';

    // The stored error is our summary of the failure. What MakeMKV or
    // HandBrake actually said is usually what identifies the cause, and it
    // used to live only in journalctl.
    const logBox = document.getElementById('errorModalLog');
    if (logBox) {
        logBox.textContent = 'Loading…';
        fetch('/api/jobs/' + jobId + '/log')
            .then(r => r.json())
            .then(d => {
                logBox.textContent = d.empty
                    ? 'No tool output was captured for this job. Jobs that ran before '
                      + 'per-job logging was added have none.'
                    : d.log;
                logBox.scrollTop = logBox.scrollHeight;
            })
            .catch(err => { logBox.textContent = 'Could not load the log: ' + err.message; });
    }

    const modal = new bootstrap.Modal(document.getElementById('errorModal'));
    modal.show();
}

// ------------------------------------------------------------------ //
// Television
//
// The season and starting episode get baked into filenames the moment
// encoding is queued, and an off-by-one silently mislabels a whole season
// that Plex then displays as the wrong episodes. So this is offered while it
// is still cheap to change, and refused once it is not.
// ------------------------------------------------------------------ //

function editSeries(jobId, season, firstEpisode, suggestedShow, suggestedYear) {
    document.getElementById('seriesJobId').value = jobId;
    document.getElementById('seriesSeason').value = season;
    document.getElementById('seriesFirstEpisode').value = firstEpisode;
    document.getElementById('seriesShowResults').innerHTML = '';
    document.getElementById('seriesShowResults').className = '';
    document.getElementById('seriesPosterUrl').value = '';
    document.getElementById('seriesTmdbId').value = '';
    // Whatever the disc label parsed to, as a starting point. It is a guess
    // from a film search, which is exactly why the TMDb lookup is offered.
    document.getElementById('seriesShowName').value = suggestedShow || '';
    document.getElementById('seriesShowYear').value = suggestedYear || '';
    document.getElementById('seriesModeHint').classList.add('d-none');
    const discHint = document.getElementById('seriesDiscHint');
    discHint.classList.add('d-none');
    discHint.textContent = '';
    previewSeries();
    new bootstrap.Modal(document.getElementById('seriesModal')).show();
    // Synchronously, inside the tap that opened the dialog: iOS raises the
    // keyboard for a focus() that happens in a user gesture and refuses one
    // that arrives later, so doing this from shown.bs.modal gets a search
    // sheet with no keyboard and a second tap needed to fix it.
    setSeriesStep('find');

    // Where the disc label says this one belongs. Asked for after the dialog
    // is already open and filled in, so a slow answer never delays it and a
    // failed one changes nothing.
    fetch(`/api/jobs/${jobId}/series-suggestion`)
        .then(r => r.json())
        .then(d => {
            if (!d.ok || !d.apply) return;
            // Still the same job? The dialog may have been closed and
            // reopened on another one while this was in flight.
            if (document.getElementById('seriesJobId').value !== String(jobId)) return;
            document.getElementById('seriesSeason').value = d.season;
            document.getElementById('seriesFirstEpisode').value = d.first_episode;
            // The show an earlier disc of this set was named as. Two discs of
            // one box set spelled two slightly different ways make two folders.
            if (d.show && !document.getElementById('seriesTmdbId').value) {
                document.getElementById('seriesShowName').value = d.show;
                document.getElementById('seriesShowYear').value = d.year || '';
                if (d.tmdb_id) document.getElementById('seriesTmdbId').value = d.tmdb_id;
                // Disc 2 of a set gets disc 1's cover, so the answer that was
                // filled in for you is one you can recognise rather than read.
                if (d.poster) document.getElementById('seriesPosterUrl').value = d.poster;
            }
            if (d.reason) {
                discHint.textContent = d.reason;
                discHint.classList.remove('d-none');
            }
            // An earlier disc of this set already answered the "which show"
            // question, so the phone should not be asked to search for it
            // again — it opens on the numbers, with the show named above them
            // and Change one tap away if the guess is wrong.
            if (d.show) setSeriesStep('confirm');
            previewSeries();
        })
        .catch(() => {});
}

// The show has to be looked up against TMDb's *TV* namespace. Identification
// runs the movie search, which for a box set returns a confident-looking film
// — so without this step a season is named after whatever film the disc label
// happened to resemble.
//
// Typing searches. The button and the Enter key still do, immediately, because
// a field that only answers to a pause has no way of saying "yes, that one,
// now" — and because a search that returned nothing has to be repeatable
// without editing the text first.
//
// fromTyping is what separates the two: a debounced search on one letter is a
// request that cannot match anything, and telling someone off for having typed
// only one letter of a word they are still typing is nonsense. From the button
// the same emptiness is a question that deserves an answer.
let _seriesSearchSeq = 0;

function searchSeriesShow(fromTyping = false) {
    const query = document.getElementById('seriesShowName').value.trim();
    const box = document.getElementById('seriesShowResults');
    if (query.length < 2) {
        box.className = '';
        if (fromTyping) { box.replaceChildren(); return; }
        box.innerHTML = '<span class="text-warning small">Enter at least two letters of the show name.</span>';
        return;
    }
    box.className = '';
    box.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Searching TMDb…';

    // Answers do not promise to arrive in the order they were asked for, and
    // now that typing asks, several are in flight at once: "The W" answered
    // after "The Wire" would paint the wider list over the better one. Only
    // the newest request may paint. The same guard as _seriesPreviewSeq, for
    // the same reason.
    const seq = ++_seriesSearchSeq;
    fetch('/api/tmdb/search-tv?query=' + encodeURIComponent(query))
        .then(r => r.json())
        .then(d => {
            if (seq !== _seriesSearchSeq) return;
            if (d.error) { box.innerHTML = `<span class="text-danger small">${escapeHtml(d.error)}</span>`; return; }
            if (!d.results.length) { box.innerHTML = '<span class="text-secondary small">No shows found.</span>'; return; }
            // Built as elements, with the handler attached in JS.
            //
            // The template-string version put JSON.stringify(s.name) inside a
            // double-quoted onclick attribute — and JSON.stringify wraps its
            // own result in double quotes, so the attribute ended at the show
            // name and every result in the list was a broken handler. Any show
            // with an apostrophe or a quote in its title, which is most of the
            // ones people search for.
            box.replaceChildren(...d.results.map(s => {
                const button = document.createElement('button');
                button.type = 'button';
                button.className =
                    'list-group-item list-group-item-action bg-transparent '
                    + 'text-start d-flex gap-2 align-items-start';

                const name = document.createElement('strong');
                name.textContent = s.name;
                const year = document.createElement('span');
                year.className = 'text-secondary';
                year.textContent = s.year ? ` (${s.year})` : '';
                const overview = document.createElement('div');
                overview.className = 'small text-secondary';
                overview.textContent = (s.overview || '').slice(0, 140);

                // The poster the API has been sending all along. Two shows of
                // the same name a few years apart is the ordinary case here —
                // that is the whole reason this list exists — and the cover is
                // what tells them apart at a glance where the year does not.
                const text = document.createElement('div');
                text.className = 'adr-poster-beside';
                text.append(name, year, overview);
                button.append(posterThumb(s.poster_url, 'tv'), text);

                button.addEventListener('click', () => pickSeriesShow(
                    s.tmdb_id, s.name, s.year || null, s.poster_url || ''));
                return button;
            }));
            box.className = 'list-group';
        })
        .catch(err => {
            if (seq !== _seriesSearchSeq) return;
            box.innerHTML = `<span class="text-danger small">${escapeHtml(err.message)}</span>`;
        });
}

const _searchSeriesShowSoon = debounce(
    () => searchSeriesShow(true), SEARCH_DEBOUNCE_MILLISECONDS);

// The show name drives two things: the filename preview, which is instant and
// local, and the TMDb lookup, which is neither.
function onSeriesShowInput() {
    previewSeries();
    _searchSeriesShowSoon();
}

// Which half of the dialog the phone is showing.
//
// Both halves are on screen at once on a desktop and always were; the step
// rules live inside the phone's media query, so this attribute is inert above
// 768px and the desktop dialog is exactly what it was. On a phone the two
// halves cannot share a screen with the keyboard up — which is the whole
// complaint this rework started from.
function setSeriesStep(step) {
    const content = document.querySelector('#seriesModal .modal-content');
    if (!content) return;
    content.dataset.step = step;

    // The confirm step asks for a season and a first episode with the search
    // gone, so it has to say a season of what.
    const chosen = document.getElementById('seriesChosenShow');
    if (chosen) {
        const name = document.getElementById('seriesShowName').value.trim();
        const year = document.getElementById('seriesShowYear').value.trim();
        document.getElementById('seriesChosenName').textContent =
            name + (year ? ` (${year})` : '');
        const slot = document.getElementById('seriesChosenPoster');
        if (slot) {
            slot.replaceChildren(posterThumb(
                document.getElementById('seriesPosterUrl').value, 'tv'));
        }
        chosen.classList.toggle('d-none', !name);
    }

    if (step === 'find') {
        const input = document.getElementById('seriesShowName');
        // preventScroll: on a phone the field is already at the top of a
        // full-screen sheet, and scrolling to it moves the sheet under the
        // keyboard that is about to appear.
        if (input) input.focus({preventScroll: true});
    }
}

function pickSeriesShow(tmdbId, name, year, posterUrl) {
    document.getElementById('seriesTmdbId').value = tmdbId;
    document.getElementById('seriesShowName').value = name;
    document.getElementById('seriesShowYear').value = year || '';
    // Carried so the confirm step can show which cover was chosen, and so
    // saving can put it on the job — naming a series by hand used to leave
    // the card with no image at all.
    document.getElementById('seriesPosterUrl').value = posterUrl || '';
    // The list is cleared rather than replaced with "Using X": the chosen-show
    // row says that now, and on a phone it is the only one of the two the
    // confirm step shows.
    const box = document.getElementById('seriesShowResults');
    box.replaceChildren();
    box.className = '';
    setSeriesStep('confirm');
    previewSeries();
}

let _seriesPreviewSeq = 0;

function previewSeries() {
    const box = document.getElementById('seriesPreview');
    const show = document.getElementById('seriesShowName').value.trim();
    const yearRaw = document.getElementById('seriesShowYear').value.trim();
    const season = parseInt(document.getElementById('seriesSeason').value, 10);
    const first = parseInt(document.getElementById('seriesFirstEpisode').value, 10);
    if (!show || isNaN(season) || isNaN(first)) { box.innerHTML = ''; return; }

    const pad = n => String(n).padStart(2, '0');
    const folder = show + (yearRaw ? ` (${yearRaw})` : '');
    const render = titles => {
        const lines = [0, 1, 2].map(i => {
            const ep = first + i;
            const name = titles[ep] ? `   ← ${titles[ep]}` : '';
            return `${folder}/Season ${pad(season)}/${folder} - S${pad(season)}E${pad(ep)}.mp4${name}`;
        });
        box.innerHTML = '<div class="small text-secondary">Files will be named:</div>'
            + '<pre class="small mb-0">' + escapeHtml(lines.join('\n')) + '\n…</pre>';
    };
    render({});

    // Real episode titles turn "is E05 the right starting point?" from a guess
    // into something checkable by eye. Best-effort — plain numbers are all Plex
    // needs, so a lookup failure changes nothing.
    const tmdbId = document.getElementById('seriesTmdbId').value;
    if (!tmdbId) return;
    // Every keystroke fires one of these, and the answers do not promise to
    // come back in order — a slow reply for season 1 landing after a fast one
    // for season 2 rendered season 1's titles under season 2's numbers. Only
    // the newest request may paint.
    const seq = ++_seriesPreviewSeq;
    fetch(`/api/tmdb/season?tmdb_id=${tmdbId}&season=${season}`)
        .then(r => r.json())
        .then(d => {
            if (seq !== _seriesPreviewSeq) return;
            if (!d.episodes || !d.episodes.length) return;
            const titles = {};
            d.episodes.forEach(e => { titles[e.episode_number] = e.name; });
            render(titles);
        })
        .catch(() => {});
}

function saveSeries() {
    const jobId = document.getElementById('seriesJobId').value;
    const payload = {
        content_type: 'series',
        season: parseInt(document.getElementById('seriesSeason').value, 10),
        first_episode: parseInt(document.getElementById('seriesFirstEpisode').value, 10),
        show: document.getElementById('seriesShowName').value.trim(),
        year: parseInt(document.getElementById('seriesShowYear').value, 10) || null,
        tmdb_id: parseInt(document.getElementById('seriesTmdbId').value, 10) || null,
        poster_url: document.getElementById('seriesPosterUrl').value || null,
    };

    // No job id: the modal was opened to start the mode rather than to fix a
    // single disc.
    if (!jobId) {
        if (!payload.show) { notify('Enter the show name first.', 'warning'); return; }
        fetch('/api/series-mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({active: true, ...payload}),
        }).then(r => r.json().then(d => ({ok: r.ok, d})))
          .then(({ok, d}) => {
              if (!ok) { notify(reasonFrom(d, 'Could not start series mode.'), 'danger'); return; }
              location.reload();
          })
          .catch(err => notify('Error: ' + err.message, 'danger'));
        return;
    }

    fetch('/api/jobs/' + jobId + '/content-type', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    })
    .then(r => r.json().then(d => ({ok: r.ok, d})))
    .then(({ok, d}) => {
        if (!ok) { notify(reasonFrom(d, 'Could not change this job.'), 'danger'); return; }
        location.reload();
    })
    .catch(err => notify('Error: ' + err.message, 'danger'));
}

// ------------------------------------------------------------------ //
// Series mode
//
// The value is not "mark this disc as a series" — it is that the episode
// counter carries across discs. Otherwise disc 2 of a season means typing the
// starting episode again, which is the number that is easy to get wrong and
// expensive to get wrong.
// ------------------------------------------------------------------ //

function startSeriesMode() {
    // The blue disc hint belongs to whichever job editSeries last opened.
    // Left on screen it explains a different disc, directly above this
    // dialog's own warning and contradicting the fields below it.
    const discHint = document.getElementById('seriesDiscHint');
    if (discHint) { discHint.classList.add('d-none'); discHint.textContent = ''; }
    // Reuses the per-job series modal: same three questions, different verb.
    document.getElementById('seriesJobId').value = '';   // '' means "the mode"
    document.getElementById('seriesPosterUrl').value = '';
    document.getElementById('seriesTmdbId').value = '';
    document.getElementById('seriesShowName').value = '';
    document.getElementById('seriesShowYear').value = '';
    document.getElementById('seriesSeason').value = 1;
    document.getElementById('seriesFirstEpisode').value = 1;
    document.getElementById('seriesShowResults').innerHTML = '';
    document.getElementById('seriesShowResults').className = '';
    document.getElementById('seriesModeHint').classList.remove('d-none');
    previewSeries();
    new bootstrap.Modal(document.getElementById('seriesModal')).show();
    // Nothing is known about the show here, so this always opens on the
    // search step — and focuses inside the tap, so the keyboard comes up with
    // the sheet rather than after another tap.
    setSeriesStep('find');
}

function stopSeriesMode() {
    confirmAction({
        title: "Turn off series mode?",
        body: 'Turn off TV series mode? Discs will be identified as films again.',
        confirmLabel: "Turn it off",
        danger: false,
    }).then(confirmed => {
        if (!confirmed) return;
        fetch('/api/series-mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({active: false}),
        }).then(() => location.reload())
          .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

function fixNextEpisode(current) {
    const value = prompt(
        'Episode number the NEXT disc should start at:\n\n'
        + 'The counter advances by however many titles each disc produced, which '
        + 'is right until a disc holds a feature-length extra that looked like an '
        + 'episode.', current);
    if (value === null) return;
    fetch('/api/series-mode/next-episode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({episode: parseInt(value, 10)}),
    }).then(r => r.json().then(d => ({ok: r.ok, d})))
      .then(({ok, d}) => {
          if (!ok) { notify(reasonFrom(d, 'Could not change the counter.'), 'danger'); return; }
          location.reload();
      })
      .catch(err => notify('Error: ' + err.message, 'danger'));
}

function markAsMovie(jobId) {
    confirmAction({
        title: "Treat as a film?",
        body: 'Treat this disc as a film again? Files will be named "Title (Year)".',
        confirmLabel: "Yes, a film",
        danger: false,
    }).then(confirmed => {
        if (!confirmed) return;
        fetch('/api/jobs/' + jobId + '/content-type', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({content_type: 'movie'}),
        })
        .then(r => r.json().then(d => ({ok: r.ok, d})))
        .then(({ok, d}) => {
            if (!ok) { notify(reasonFrom(d, 'Could not change this job.'), 'danger'); return; }
            location.reload();
        })
        .catch(err => notify('Error: ' + err.message, 'danger'));
    });
}

// ------------------------------------------------------------------ //
// Retry
//
// A rip is forty minutes and several GB. Most failures happen after the
// expensive part, so the first thing to establish is which part still exists
// — "moves the finished file" and "re-encodes for forty minutes" deserve
// different answers from the user.
// ------------------------------------------------------------------ //

function retryJob(jobId) {
    fetch('/api/jobs/' + jobId + '/retry')
        .then(r => r.json())
        .then(plan => {
            if (!plan.can_retry) {
                // A refusal, not a success. Green and auto-hiding meant the
                // reason a retry is impossible — usually that nothing is left
                // on disk — flashed by in the colour used for "done".
                notify(plan.reason, 'warning');
                return;
            }
            confirmAction({
                title: "Retry this job?",
                body: plan.reason + '\n\nGo ahead?',
                confirmLabel: "Retry",
                danger: false,
            }).then(confirmed => {
                if (!confirmed) return;

                return fetch('/api/jobs/' + jobId + '/retry', {method: 'POST'})
                    .then(r => r.json().then(d => ({ok: r.ok, d})))
                    .then(({ok, d}) => {
                        if (ok && d.ok) location.reload();
                        else notify(reasonFrom(d, 'Retry failed.'), 'danger');
                    });
            });
})
        .catch(err => notify('Error: ' + err.message, 'danger'));
}

function copyErrorText(btn) {
    // Both halves: the summary is what we concluded, the log is the evidence.
    const text = document.getElementById('errorModalText').textContent
        + '\n\n--- tool output ---\n'
        + (document.getElementById('errorModalLog')?.textContent || '');
    copyToClipboard(
        text,
        btn,
        '<i class="bi bi-check me-1"></i>Copied!',
    );
}

// ------------------------------------------------------------------ //
// System stats (navbar mini-bars)
// ------------------------------------------------------------------ //

function updateSysBar(barId, valId, percent, label) {
    const bar = document.getElementById(barId);
    const val = document.getElementById(valId);
    if (!bar || !val) return;
    bar.style.width = percent + '%';
    bar.className = 'sys-bar-fill';
    if (percent > 85) bar.classList.add('crit');
    else if (percent > 65) bar.classList.add('warn');
    val.textContent = label || (Math.round(percent) + '%');
}

function refreshSystemStats() {
    return fetch('/api/system')
        .then(r => r.json())
        .then(data => {
            updateSysBar('cpuBar', 'cpuVal', data.cpu_percent);
            if (data.ram) {
                updateSysBar('ramBar', 'ramVal', data.ram.percent,
                    data.ram.used_gb.toFixed(1) + '/' + data.ram.total_gb.toFixed(0) + 'G');
            }
            if (data.disk) {
                updateSysBar('diskBar', 'diskVal', data.disk.percent,
                    data.disk.free_gb.toFixed(0) + 'G free');
            }
            if (data.gpu) {
                const gpuStat = document.getElementById('gpuStat');
                if (gpuStat) gpuStat.classList.remove('d-none');
                updateSysBar('gpuBar', 'gpuVal', data.gpu.utilization);
            }
        })
        .catch(err => { throw err; });
}

// ------------------------------------------------------------------ //
// Init
// ------------------------------------------------------------------ //

// ------------------------------------------------------------------ //
// Elapsed timer for active jobs
// ------------------------------------------------------------------ //

function updateElapsedTimers() {
    document.querySelectorAll('.elapsed-timer[data-start]').forEach(el => {
        const iso = el.dataset.start;
        if (!iso) { el.textContent = '--:--'; return; }
        const startMs = new Date(iso).getTime();
        if (isNaN(startMs)) { el.textContent = '--:--'; return; }
        const elapsed = Math.max(0, Math.floor((Date.now() - startMs) / 1000));
        const h = Math.floor(elapsed / 3600);
        const m = Math.floor((elapsed % 3600) / 60);
        const s = elapsed % 60;
        const pad = n => String(n).padStart(2, '0');
        el.textContent = h > 0
            ? `${h}:${pad(m)}:${pad(s)}`
            : `${pad(m)}:${pad(s)}`;
    });
}

// ------------------------------------------------------------------ //
// Init
// ------------------------------------------------------------------ //

// ------------------------------------------------------------------ //
// Optical-drive health
//
// The host's drives are always visible through /sys, but the device node only
// exists in here if the passthrough applied at container start. When it did
// not, the dashboard would otherwise show no drives and no reason why.
// ------------------------------------------------------------------ //

function refreshDriveHealth() {
    const box = document.getElementById('driveHealth');
    if (!box) return Promise.resolve();
    return fetch('/api/drives/health')
        .then(r => r.json())
        .then(d => {
            if (!d.problems || d.problems.length === 0) { box.innerHTML = ''; return; }
            box.innerHTML = d.problems.map(p =>
                `<div class="alert alert-danger d-flex align-items-start">
                   <i class="bi bi-exclamation-octagon-fill me-2 mt-1"></i>
                   <div>${escapeHtml(p)}</div>
                 </div>`
            ).join('');
        })
        .catch(err => { throw err; });
}

// ------------------------------------------------------------------ //
// Doctor badge in the navbar.
//
// A problem you only see on the page that reports it is a problem you find
// after the failed rip, so the count rides along on every page. The Doctor page
// keeps its own list up to date and sets the badge itself.
// ------------------------------------------------------------------ //

function refreshDoctorBadge() {
    // Both of them: the topbar's, and the one on the bottom bar's Doctor tab.
    // The phone only ever sees the second, and a count that appears on the
    // desktop and not on the device the application is driven from is a count
    // nobody reads.
    const badges = ['doctorBadge', 'doctorBadgeMobile']
        .map(id => document.getElementById(id))
        .filter(Boolean);
    if (!badges.length || window.location.pathname === '/doctor') return Promise.resolve();
    return fetch('/api/doctor')
        .then(r => r.json())
        .then(d => {
            badges.forEach(badge => {
                badge.textContent = d.failing;
                badge.classList.toggle('d-none', d.failing === 0);
            });
        })
        .catch(err => { throw err; });
}

// ------------------------------------------------------------------ //
// The Settings tab strip, on a phone
//
// Five tabs are about 470px of labels in a 390px window, so below md the strip
// scrolls sideways instead of wrapping. That solves the layout and creates a
// smaller problem: the tab you are on can be off the edge of it — Advanced is,
// on a fresh load, and after a reload following a save it is the one you were
// working in. So the active tab is put in the middle of the strip, at load and
// on every switch. Sideways, only: block 'nearest' stops it dragging the page
// up and down as well.
// ------------------------------------------------------------------ //

function keepTheActiveSettingsTabInView() {
    const strip = document.getElementById('settingsTabs');
    if (!strip) return;
    const centre = () => {
        const active = strip.querySelector('.nav-link.active');
        if (active) active.scrollIntoView({inline: 'center', block: 'nearest'});
    };
    centre();
    strip.addEventListener('shown.bs.tab', centre);
}

document.addEventListener('DOMContentLoaded', () => {
    keepTheActiveSettingsTabInView();

    // Start auto-refresh if on dashboard
    if (window.location.pathname === '/') {
        // Once immediately: the detail line is rendered empty by the server,
        // so waiting for the first tick leaves the card blank for five
        // seconds every time the page loads.
        refreshDashboard();
        setInterval(() => pollWithoutStacking('dashboard', refreshDashboard), REFRESH_INTERVAL);
        // Start elapsed timers — tick every second
        updateElapsedTimers();
        setInterval(updateElapsedTimers, 1000);
        // Optical-drive passthrough health
        refreshDriveHealth();
        setInterval(() => pollWithoutStacking('drives', refreshDriveHealth), 15000);
    }

    // System stats — poll every 5 seconds on all pages
    refreshSystemStats();
    setInterval(() => pollWithoutStacking('system', refreshSystemStats), 5000);

    // Doctor badge — cheap local checks, no need to poll hard
    refreshDoctorBadge();
    setInterval(() => pollWithoutStacking('doctor', refreshDoctorBadge), 60000);
});
