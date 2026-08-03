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
    document.getElementById('errorModalText').textContent = errorText || 'No error message stored.';
    const modal = new bootstrap.Modal(document.getElementById('errorModal'));
    modal.show();
}

function copyErrorText() {
    const text = document.getElementById('errorModalText').textContent;
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
