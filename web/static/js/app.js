/**
 * Automatic Disc Ripper – Dashboard auto-refresh and utility functions.
 */

// ------------------------------------------------------------------ //
// Auto-refresh active jobs every 5 seconds
// ------------------------------------------------------------------ //

const REFRESH_INTERVAL = 5000;

function refreshDashboard() {
    // Only refresh on the dashboard page
    if (window.location.pathname !== '/') return;

    fetch('/api/jobs/active')
        .then(r => r.json())
        .then(activeJobs => {

            // Detect new jobs that don't have a card yet → full reload
            const displayedIds = new Set(
                Array.from(document.querySelectorAll('[data-job-id]'))
                    .map(el => parseInt(el.dataset.jobId))
            );
            const hasNewJob = activeJobs.some(j => !displayedIds.has(j.id));
            if (hasNewJob) {
                location.reload();
                return;
            }

            updateActiveJobs(activeJobs);
            updateQueueSize();
        })
        .catch(err => console.warn('Refresh failed:', err));
}

function updateActiveJobs(jobs) {
    jobs.forEach(job => {
        const card = document.querySelector(`[data-job-id="${job.id}"]`);
        if (!card) return; // New job appeared – full page reload needed

        // If status changed (e.g. ripped→encoding), reload to get correct layout
        const prevStatus = card.dataset.jobStatus;
        if (prevStatus && prevStatus !== job.status) {
            location.reload();
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
            const pct = ((rawPct || 0) * 100).toFixed(1);
            bar.style.width = pct + '%';
            bar.textContent = pct + '%';
            bar.setAttribute('aria-valuenow', pct);

            // Update bar colour based on status
            bar.className = 'progress-bar progress-bar-striped progress-bar-animated';
            if (job.status === 'ripping') bar.classList.add('bg-warning');
            else if (job.status === 'encoding') bar.classList.add('bg-info');
            else bar.classList.add('bg-primary');
        }

        // Update status badge
        const badge = card.querySelector('.badge');
        if (badge && job.status !== 'ripped') {
            badge.textContent = capitalize(job.status);
            badge.className = 'badge';
            if (job.status === 'ripping') badge.classList.add('bg-warning', 'text-dark');
            else if (job.status === 'encoding') badge.classList.add('bg-info', 'text-dark');
            else badge.classList.add('bg-primary');
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
    });

    // If a job finished (no longer in active list), reload the page
    const displayedIds = Array.from(document.querySelectorAll('[data-job-id]'))
        .map(el => parseInt(el.dataset.jobId));
    const activeIds = jobs.map(j => j.id);
    const finishedAny = displayedIds.some(id => !activeIds.includes(id));
    if (finishedAny) {
        setTimeout(() => location.reload(), 500);
    }
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
    if (!confirm('Are you sure you want to cancel job #' + jobId + '?')) return;

    fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                location.reload();
            } else {
                alert('Could not cancel: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
}

function toggleDrive(driveLetter, disable) {
    const action = disable ? 'disable' : 'enable';
    if (!confirm(`Do you want to ${action} drive ${driveLetter}?`)) return;

    fetch(`/api/drives/${encodeURIComponent(driveLetter)}/toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ disabled: disable }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.ok) location.reload();
            else alert('Could not change: ' + (data.error || 'Unknown error'));
        })
        .catch(err => alert('Error: ' + err.message));
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
    fetch(`/api/drives/${encodeURIComponent(driveLetter)}/rip`, { method: 'POST' })
        .then(r => r.json().then(d => ({ ok: r.ok, d })))
        .then(({ ok, d }) => {
            if (!ok) { alert(d.message || 'Could not start the rip.'); return; }
            setTimeout(() => location.reload(), 800);
        })
        .catch(err => alert('Error: ' + err.message));
}

// ------------------------------------------------------------------ //
// Drive eject toggle
// ------------------------------------------------------------------ //

function toggleEject(driveLetter, enable) {
    const label = enable ? 'enable auto-eject' : 'disable auto-eject';
    if (!confirm(`Do you want to ${label} for drive ${driveLetter}?`)) return;

    fetch(`/api/drives/${encodeURIComponent(driveLetter)}/eject-toggle`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ auto_eject: enable }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.ok) location.reload();
            else alert('Could not change eject setting: ' + (data.error || 'Unknown error'));
        })
        .catch(err => alert('Error: ' + err.message));
}

// ------------------------------------------------------------------ //
// Drive eject (open tray)
// ------------------------------------------------------------------ //

function ejectDrive(driveLetter) {
    fetch(`/api/drives/${encodeURIComponent(driveLetter)}/eject`, {
        method: 'POST',
    })
        .then(r => r.json())
        .then(data => {
            if (!data.ok) {
                alert('Could not eject: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
}

// ------------------------------------------------------------------ //
// Drive label
// ------------------------------------------------------------------ //

function saveDriveLabel(driveLetter) {
    const input = document.getElementById('driveLabel_' + driveLetter.replace(':', ''));
    if (!input) return;
    const label = input.value.trim();

    fetch(`/api/drives/${encodeURIComponent(driveLetter)}/label`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ label: label }),
    })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                location.reload();
            } else {
                alert('Could not save label: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
}

// ------------------------------------------------------------------ //
// Clear history
// ------------------------------------------------------------------ //

function clearHistory() {
    if (!confirm('Clear all completed, failed, and cancelled jobs from history?')) return;

    fetch('/api/history/clear', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                alert(`${data.deleted} jobs deleted.`);
                location.reload();
            } else {
                alert('Could not clear: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
}

function deleteJob(jobId) {
    if (!confirm('Delete job #' + jobId + '?')) return;

    fetch(`/api/jobs/${jobId}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                const row = document.querySelector(`tr[data-status] td:first-child`);
                // Simple approach: reload page
                location.reload();
            } else {
                alert('Could not delete: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
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
                alert('Could not change Plex flag: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
}

function moveToPlexManual(jobId) {
    if (!confirm('Move files to the Plex folder?')) return;
    fetch(`/api/jobs/${jobId}/move-to-plex`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
    })
        .then(r => r.json())
        .then(data => {
            if (data.ok) {
                location.reload();
            } else {
                alert('Could not move: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
}

// ------------------------------------------------------------------ //
// TMDb Re-match modal
// ------------------------------------------------------------------ //

function openRematchModal(jobId, currentTitle) {
    document.getElementById('rematchJobId').value = jobId;
    document.getElementById('rematchJobTitle').textContent = currentTitle;
    document.getElementById('rematchQuery').value = currentTitle.replace(/\s*\(\d{4}\)\s*$/, '');
    document.getElementById('rematchYear').value = '';
    document.getElementById('rematchResults').innerHTML = '';
    document.getElementById('rematchEmpty').classList.add('d-none');
    const modal = new bootstrap.Modal(document.getElementById('rematchModal'));
    modal.show();
}

function searchTmdb() {
    const query = document.getElementById('rematchQuery').value.trim();
    if (!query) return;
    const year = document.getElementById('rematchYear').value.trim();
    const resultsDiv = document.getElementById('rematchResults');
    const spinner = document.getElementById('rematchSpinner');
    const empty = document.getElementById('rematchEmpty');

    resultsDiv.innerHTML = '';
    empty.classList.add('d-none');
    spinner.classList.remove('d-none');

    let url = `/api/tmdb/search?q=${encodeURIComponent(query)}`;
    if (year) url += `&year=${year}`;

    fetch(url)
        .then(r => r.json())
        .then(data => {
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
                col.className = 'col-6 col-md-4';
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
            spinner.classList.add('d-none');
            empty.textContent = 'Search error: ' + err.message;
            empty.classList.remove('d-none');
        });
}

function applyRematch(tmdbId) {
    const jobId = document.getElementById('rematchJobId').value;
    if (!confirm('Re-match this job to the selected movie?')) return;

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
                alert('Could not re-match: ' + (data.error || 'Unknown error'));
            }
        })
        .catch(err => alert('Error: ' + err.message));
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

    if (pi.phase === 'ripping') {
        const parts = [];
        if (pi.title_total > 0) {
            parts.push(`Title ${pi.title_current}/${pi.title_total}`);
        }
        if (pi.title_progress != null) {
            parts.push((pi.title_progress * 100).toFixed(1) + '%');
        }
        if (parts.length === 0 && pi.description) {
            return pi.description;
        }
        return parts.join(' \u00b7 ');
    }

    if (pi.phase === 'encoding') {
        if (pi.state === 'scanning') {
            return 'Scanning video\u2026';
        }
        if (pi.state === 'muxing') {
            return 'Muxing MP4\u2026';
        }
        const parts = [];
        if (pi.track_total > 1) {
            parts.push(`Title ${pi.track_current}/${pi.track_total}`);
        }
        if (pi.track_progress != null) {
            parts.push((pi.track_progress * 100).toFixed(1) + '%');
        }
        if (pi.pass_total > 1) {
            parts.push(`Pass ${pi.pass_num + 1}/${pi.pass_total}`);
        }
        if (pi.eta_seconds > 0) {
            parts.push('~' + formatEta(pi.eta_seconds));
        }
        if (pi.fps > 0) {
            parts.push(pi.fps.toFixed(1) + ' fps');
        }
        return parts.join(' \u00b7 ');
    }

    return '';
}

function formatEta(seconds) {
    if (seconds <= 0) return '';
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    const s = seconds % 60;
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s}s`;
    return `${s}s`;
}

function escapeHtml(str) {
    const el = document.createElement('span');
    el.textContent = str || '';
    return el.innerHTML;
}

function copyPath(path) {
    navigator.clipboard.writeText(path).then(() => {
        // Brief visual feedback on the button
        const btn = event.target.closest('button');
        if (btn) {
            const orig = btn.innerHTML;
            btn.innerHTML = '<i class="bi bi-check"></i>';
            setTimeout(() => btn.innerHTML = orig, 1500);
        }
    });
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
    video.src = `/api/jobs/${jobId}/stream/${encodeURIComponent(filename)}`;
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
    document.getElementById('seriesTmdbId').value = '';
    // Whatever the disc label parsed to, as a starting point. It is a guess
    // from a film search, which is exactly why the TMDb lookup is offered.
    document.getElementById('seriesShowName').value = suggestedShow || '';
    document.getElementById('seriesShowYear').value = suggestedYear || '';
    document.getElementById('seriesModeHint').classList.add('d-none');
    previewSeries();
    new bootstrap.Modal(document.getElementById('seriesModal')).show();
}

// The show has to be looked up against TMDb's *TV* namespace. Identification
// runs the movie search, which for a box set returns a confident-looking film
// — so without this step a season is named after whatever film the disc label
// happened to resemble.
function searchSeriesShow() {
    const query = document.getElementById('seriesShowName').value.trim();
    const box = document.getElementById('seriesShowResults');
    if (!query) { box.innerHTML = '<span class="text-warning small">Enter a show name first.</span>'; return; }
    box.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Searching TMDb…';

    fetch('/api/tmdb/search-tv?query=' + encodeURIComponent(query))
        .then(r => r.json())
        .then(d => {
            if (d.error) { box.innerHTML = `<span class="text-danger small">${escapeHtml(d.error)}</span>`; return; }
            if (!d.results.length) { box.innerHTML = '<span class="text-secondary small">No shows found.</span>'; return; }
            box.innerHTML = d.results.map(s => `
                <button type="button" class="list-group-item list-group-item-action bg-transparent text-start"
                        onclick="pickSeriesShow(${s.tmdb_id}, ${JSON.stringify(s.name)}, ${s.year || 'null'})">
                    <strong>${escapeHtml(s.name)}</strong>
                    <span class="text-secondary">${s.year ? '(' + s.year + ')' : ''}</span>
                    <div class="small text-secondary">${escapeHtml((s.overview || '').slice(0, 140))}</div>
                </button>`).join('');
            box.className = 'list-group';
        })
        .catch(err => { box.innerHTML = `<span class="text-danger small">${escapeHtml(err.message)}</span>`; });
}

function pickSeriesShow(tmdbId, name, year) {
    document.getElementById('seriesTmdbId').value = tmdbId;
    document.getElementById('seriesShowName').value = name;
    document.getElementById('seriesShowYear').value = year || '';
    document.getElementById('seriesShowResults').innerHTML =
        `<div class="small text-success"><i class="bi bi-check-circle me-1"></i>Using
         <strong>${escapeHtml(name)}</strong>${year ? ' (' + year + ')' : ''}</div>`;
    document.getElementById('seriesShowResults').className = '';
    previewSeries();
}

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
    fetch(`/api/tmdb/season?tmdb_id=${tmdbId}&season=${season}`)
        .then(r => r.json())
        .then(d => {
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
    };

    // No job id: the modal was opened to start the mode rather than to fix a
    // single disc.
    if (!jobId) {
        if (!payload.show) { alert('Enter the show name first.'); return; }
        fetch('/api/series-mode', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({active: true, ...payload}),
        }).then(r => r.json().then(d => ({ok: r.ok, d})))
          .then(({ok, d}) => {
              if (!ok) { alert(d.error || 'Could not start series mode.'); return; }
              location.reload();
          })
          .catch(err => alert('Error: ' + err.message));
        return;
    }

    fetch('/api/jobs/' + jobId + '/content-type', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
    })
    .then(r => r.json().then(d => ({ok: r.ok, d})))
    .then(({ok, d}) => {
        if (!ok) { alert(d.error || 'Could not change this job.'); return; }
        location.reload();
    })
    .catch(err => alert('Error: ' + err.message));
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
    // Reuses the per-job series modal: same three questions, different verb.
    document.getElementById('seriesJobId').value = '';   // '' means "the mode"
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
}

function stopSeriesMode() {
    if (!confirm('Turn off TV series mode? Discs will be identified as films again.')) return;
    fetch('/api/series-mode', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({active: false}),
    }).then(() => location.reload())
      .catch(err => alert('Error: ' + err.message));
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
          if (!ok) { alert(d.error || 'Could not change the counter.'); return; }
          location.reload();
      })
      .catch(err => alert('Error: ' + err.message));
}

function markAsMovie(jobId) {
    if (!confirm('Treat this disc as a film again? Files will be named "Title (Year)".')) return;
    fetch('/api/jobs/' + jobId + '/content-type', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content_type: 'movie'}),
    })
    .then(r => r.json().then(d => ({ok: r.ok, d})))
    .then(({ok, d}) => {
        if (!ok) { alert(d.error || 'Could not change this job.'); return; }
        location.reload();
    })
    .catch(err => alert('Error: ' + err.message));
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
                alert(plan.reason);
                return;
            }
            if (!confirm(plan.reason + '\n\nGo ahead?')) return;

            return fetch('/api/jobs/' + jobId + '/retry', {method: 'POST'})
                .then(r => r.json().then(d => ({ok: r.ok, d})))
                .then(({ok, d}) => {
                    if (ok && d.ok) location.reload();
                    else alert(d.message || d.error || 'Retry failed.');
                });
        })
        .catch(err => alert('Error: ' + err.message));
}

function copyErrorText() {
    // Both halves: the summary is what we concluded, the log is the evidence.
    const text = document.getElementById('errorModalText').textContent
        + '\n\n--- tool output ---\n'
        + (document.getElementById('errorModalLog')?.textContent || '');
    navigator.clipboard.writeText(text).then(() => {
        const btn = event.target.closest('button');
        const orig = btn.innerHTML;
        btn.innerHTML = '<i class="bi bi-check me-1"></i>Copied!';
        setTimeout(() => btn.innerHTML = orig, 2000);
    });
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
    fetch('/api/system')
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
        .catch(() => {});
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
    if (!box) return;
    fetch('/api/drives/health')
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
        .catch(() => {});
}

// ------------------------------------------------------------------ //
// Doctor badge in the navbar.
//
// A problem you only see on the page that reports it is a problem you find
// after the failed rip, so the count rides along on every page. The Doctor page
// keeps its own list up to date and sets the badge itself.
// ------------------------------------------------------------------ //

function refreshDoctorBadge() {
    const badge = document.getElementById('doctorBadge');
    if (!badge || window.location.pathname === '/doctor') return;
    fetch('/api/doctor')
        .then(r => r.json())
        .then(d => {
            badge.textContent = d.failing;
            badge.classList.toggle('d-none', d.failing === 0);
        })
        .catch(() => {});
}

document.addEventListener('DOMContentLoaded', () => {
    // Start auto-refresh if on dashboard
    if (window.location.pathname === '/') {
        setInterval(refreshDashboard, REFRESH_INTERVAL);
        // Start elapsed timers — tick every second
        updateElapsedTimers();
        setInterval(updateElapsedTimers, 1000);
        // Optical-drive passthrough health
        refreshDriveHealth();
        setInterval(refreshDriveHealth, 15000);
    }

    // System stats — poll every 5 seconds on all pages
    refreshSystemStats();
    setInterval(refreshSystemStats, 5000);

    // Doctor badge — cheap local checks, no need to poll hard
    refreshDoctorBadge();
    setInterval(refreshDoctorBadge, 60000);
});
