document.addEventListener('DOMContentLoaded', () => {
    let API_KEY = '';

    // Fetch api_key_secret for protected endpoints (via dashboard auth session)
    fetch('api/config').then(r => r.json()).then(cfg => {
        API_KEY = cfg.api_key_secret || '';
    }).catch(() => {});

    // Intercept fetch: auto attach X-API-Key for protected POST endpoints
        const _origFetch = window.fetch;
    window.fetch = async (input, init = {}) => {
        let url = typeof input === 'string' ? input : (input && input.url ? input.url : '');
        url = url.startsWith('/') ? url : '/' + url;
        const method = (init && init.method) || 'GET';
        // Enforce X-API-Key cho moi non-GET request (POST/PUT/DELETE/PATCH), truoc GET/HEAD/OPTIONS
        const needsKey = !['GET', 'HEAD', 'OPTIONS'].includes(method.toUpperCase());
        if (needsKey) {
            if (!API_KEY) {
                await fetch('api/config').then(r => r.json()).then(cfg => { API_KEY = cfg.api_key_secret || ''; }).catch(()=>{});
            }
            if (API_KEY) {
                init.headers = { ...(init.headers || {}), 'X-API-Key': API_KEY };
            }
        }
        return _origFetch(input, init);
    };
    // Hook XMLHttpRequest — tự động gán X-API-Key cho POST/DELETE (upload dùng XHR, không qua fetch wrapper)
    (function () {
        const _open = XMLHttpRequest.prototype.open;
        const _send = XMLHttpRequest.prototype.send;
        XMLHttpRequest.prototype.open = function (method) {
            this._xhr_method = method;
            return _open.apply(this, arguments);
        };
        XMLHttpRequest.prototype.send = function () {
            const m = (this._xhr_method || "GET").toUpperCase();
            if (!["GET", "HEAD", "OPTIONS"].includes(m)) {
                const needKey = !["GET", "HEAD", "OPTIONS"].includes(m);
                if (needKey) {
                    try { this.setRequestHeader("X-API-Key", API_KEY || ""); } catch (e) {}
                }
            }
            return _send.apply(this, arguments);
        };
    })();

    // Navigation Tabs (Desktop & Mobile)
    const navItems = document.querySelectorAll('.nav-item, .mobile-nav-item');
    const tabPages = document.querySelectorAll('.tab-page');
    const pageTitleText = document.getElementById('pageTitleText');

    const tabTitles = {
        'dashboard': 'Bảng Điều Khiển Stream',
        'config': 'Cấu Hình Luồng Stream',
        'media': 'Quản Lý Video Playlist',
        'logs': 'Nhật Ký FFmpeg Core'
    };

    navItems.forEach(item => {
        item.addEventListener('click', (e) => {
            e.preventDefault();
            const targetTab = item.getAttribute('data-tab');

            document.querySelectorAll('.nav-item, .mobile-nav-item').forEach(n => {
                if (n.getAttribute('data-tab') === targetTab) {
                    n.classList.add('active');
                } else {
                    n.classList.remove('active');
                }
            });

            tabPages.forEach(p => p.classList.remove('active'));
            const page = document.getElementById(`tab-${targetTab}`);
            if (page) page.classList.add('active');

            if (pageTitleText && tabTitles[targetTab]) {
                pageTitleText.textContent = tabTitles[targetTab];
            }

            if (targetTab === 'config') loadConfig();
            if (targetTab === 'media') loadMediaList();
            if (targetTab === 'logs') loadLogs();

            window.scrollTo({ top: 0, behavior: 'smooth' });
        });
    });

    // Control Buttons
    const btnStart = document.getElementById('btnStartStream');
    const btnStop = document.getElementById('btnStopStream');
    const btnRestart = document.getElementById('btnRestartStream');

    btnStart.addEventListener('click', async () => {
        btnStart.disabled = true;
        try {
            const res = await fetch('api/stream/start', { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Lỗi khởi chạy');
            showToast('Khởi chạy stream thành công!', 'emerald');
        } catch (err) {
            showToast(err.message, 'rose');
        } finally {
            fetchTelemetry();
        }
    });

    btnStop.addEventListener('click', async () => {
        btnStop.disabled = true;
        try {
            const res = await fetch('api/stream/stop', { method: 'POST' });
            const data = await res.json();
            showToast('Đã dừng stream.', 'rose');
        } catch (err) {
            showToast(err.message, 'rose');
        } finally {
            fetchTelemetry();
        }
    });

    btnRestart.addEventListener('click', async () => {
        try {
            await fetch('api/stream/restart', { method: 'POST' });
            showToast('Đã khởi động lại luồng stream!', 'amber');
        } catch (err) {
            showToast(err.message, 'rose');
        } finally {
            fetchTelemetry();
        }
    });

    // Auto Fetch Stream Key Form
    const autoFetchForm = document.getElementById('autoFetchForm');
    const autoFetchBtn = document.getElementById('btnAutoFetchKey');

    // Extract Session ID button
    const btnExtract = document.getElementById('btnExtractSession');
    if (btnExtract) {
        btnExtract.addEventListener('click', async () => {
            btnExtract.disabled = true;
            btnExtract.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang tìm Session ID...';
            document.getElementById('extractSessionStatus').textContent = 'Đang quét Chromium/Firefox profile...';

            try {
                const res = await fetch('api/tiktok/extract-session', { method: 'POST' });
                const data = await res.json();
                if (data.success) {
                    document.getElementById('cfgSessionId').value = data.full_session;
                    document.getElementById('extractSessionStatus').textContent =
                        `✅ Đã lấy Session ID qua ${data.method === 'chromium_profile' ? 'Chromium' : 'Playwright'} (${data.elapsed_seconds}s)`;
                    showToast('Đã lấy Session ID tự động!', 'green');
                } else {
                    document.getElementById('extractSessionStatus').textContent = '❌ ' + data.message;
                    showToast('Không tìm thấy Session ID. Vui lòng thử nhập thủ công.', 'rose');
                }
            } catch (err) {
                document.getElementById('extractSessionStatus').textContent = '❌ Lỗi kết nối';
                showToast('Lỗi khi lấy Session ID: ' + err.message, 'rose');
            } finally {
                btnExtract.disabled = false;
                btnExtract.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i> Tự Động Lấy Session ID';
            }
        });
    }
    autoFetchForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const sessionId = document.getElementById('cfgSessionId').value.trim();
        if (!sessionId) {
            showToast('Vui lòng nhập TikTok Session ID!', 'rose');
            return;
        }

        const btnFetch = document.getElementById('btnAutoFetchKey');
        btnFetch.disabled = true;
        btnFetch.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang kết xuất Stream Key từ TikTok...';

        try {
            const res = await fetch('api/tiktok/fetch-stream-key', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId })
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || 'Lỗi kết xuất Stream Key');

            showToast(data.message || 'Lấy Stream Key thành công!', 'emerald');
            
            // Update manual config form fields immediately
            document.getElementById('cfgRtmpUrl').value = data.rtmp_url;
            document.getElementById('cfgStreamKey').value = data.stream_key;
            fetchTelemetry();
        } catch (err) {
            showToast(err.message, 'rose');
        } finally {
            btnFetch.disabled = false;
            btnFetch.innerHTML = '<i class="fa-solid fa-key"></i> TỰ ĐỘNG LẤY STREAM KEY & LƯU CẤU HÌNH';
        }
    });

    // Manual Config Form
    const configForm = document.getElementById('configForm');
    configForm.addEventListener('submit', async (e) => {
        e.preventDefault();
        const avatarOverlay = document.getElementById('cfgAvatarOverlay').checked;
        const payload = {
            rtmp_url: document.getElementById('cfgRtmpUrl').value,
            stream_key: document.getElementById('cfgStreamKey').value,
            resolution: document.getElementById('cfgResolution').value,
            video_bitrate: document.getElementById('cfgVideoBitrate').value,
            overlay_text: document.getElementById('cfgOverlayText').value,
            loop: document.getElementById('cfgLoop').checked,
            auto_reconnect: document.getElementById('cfgAutoReconnect').checked,
            show_clock: document.getElementById('cfgShowClock').checked,
            overlay_enabled: true,
            overlay_config: { avatar_overlay: avatarOverlay }
        };

        try {
            const res = await fetch('api/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            showToast('Đã lưu cấu hình stream!', 'emerald');
            fetchTelemetry();
        } catch (err) {
            showToast(err.message, 'rose');
        }
    });

    // Quick toggle avatar overlay (separate API call, no full config save needed)
    document.getElementById('cfgAvatarOverlay').addEventListener('change', async function(e) {
        const enabled = e.target.checked;
        try {
            const res = await fetch('api/overlay/config', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ avatar_overlay: enabled })
            });
            const data = await res.json();
            if (data.success) {
                showToast(
                    enabled
                        ? 'Avatar overlay đã bật! (Restart stream để áp dụng)'
                        : 'Avatar overlay đã tắt!',
                    enabled ? 'emerald' : 'amber'
                );
            } else {
                showToast(data.message || 'Lỗi cấu hình overlay', 'rose');
                e.target.checked = !enabled;
            }
        } catch (err) {
            showToast('Lỗi kết nối: ' + err.message, 'rose');
            e.target.checked = !enabled;
        }
    });

    // File Upload (Chunked for large file / mobile compatibility)
    let uploadActive = false;
    const CHUNK_SIZE = 16 * 1024 * 1024; // 16MB per chunk
    const fileUploadInput = document.getElementById('fileUploadInput');
    fileUploadInput.addEventListener('change', async (e) => {
        const file = e.target.files[0];
        if (!file) return;

        uploadActive = true;
        const totalChunks = Math.ceil(file.size / CHUNK_SIZE);
        let uploadedBytes = 0;
        let activeToast = showToast(`Đang tải lên ${file.name}... 0%`, 'cyan');

        for (let i = 0; i < totalChunks; i++) {
            const start = i * CHUNK_SIZE;
            const end = Math.min(start + CHUNK_SIZE, file.size);
            const chunk = file.slice(start, end);

            let retryCount = 0;
            let success = false;
            while (retryCount < 3 && !success) {
                try {
                    await uploadChunkXHR(chunk, file.name, i, totalChunks);
                    uploadedBytes += (end - start);
                    success = true;
                    const percent = Math.round((uploadedBytes / file.size) * 100);
                    activeToast = showToast(`Đang tải lên ${file.name}: ${percent}%`, 'cyan');
                } catch (err) {
                    retryCount++;
                    if (retryCount >= 3) {
                        uploadActive = false;
                        fileUploadInput.value = '';
                        clearActiveToast();
                        showToast(`Lỗi tải chunk ${i + 1}/${totalChunks}: ${err.message}. Vui lòng thử lại.`, 'rose');
                        return;
                    }
                    await new Promise(r => setTimeout(r, 1000));
                }
            }
        }

        uploadActive = false;
        fileUploadInput.value = '';
        clearActiveToast();
        showToast(`Tải lên ${file.name} thành công!`, 'emerald');
        loadMediaList();
        setTimeout(() => loadMediaList(), 5000);
    });

    function uploadChunkXHR(chunk, filename, chunkIndex, totalChunks) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            const url = `api/media/upload-chunk?filename=${encodeURIComponent(filename)}&chunkIndex=${chunkIndex}&totalChunks=${totalChunks}`;
            xhr.open('POST', url);

            xhr.upload.onprogress = (event) => {
                if (event.lengthComputable) {
                    const overall = Math.round(((chunkIndex * CHUNK_SIZE + event.loaded) / (totalChunks * CHUNK_SIZE)) * 100);
                    showToast(`Đang tải lên ${filename}: ${Math.min(overall, 100)}%`, 'cyan');
                }
            };

            xhr.timeout = 600000; // 10 minutes per chunk
            xhr.onload = () => {
                if (xhr.status === 200) {
                    let data;
                    try { data = JSON.parse(xhr.responseText); } catch (e) {}
                    resolve(data);
                } else if (xhr.status === 413) {
                    reject(new Error('File quá lớn (max 10GB)'));
                } else if (xhr.status === 0) {
                    reject(new Error('Kết nối bị gián đoạn'));
                } else {
                    let detail = 'Không xác định';
                    try { detail = JSON.parse(xhr.responseText).detail; } catch (e) {}
                    reject(new Error(`Lỗi (${xhr.status}): ${detail}`));
                }
            };
            xhr.onerror = () => reject(new Error('Lỗi kết nối'));
            xhr.ontimeout = () => reject(new Error('Quá thời gian'));
            xhr.send(chunk);
        });
    }

    // Prevent accidental navigation during upload
    window.addEventListener('beforeunload', (e) => {
        if (uploadActive) {
            e.preventDefault();
            e.returnValue = '';
        }
    });

    // Telemetry Polling (every 2s)
    async function fetchTelemetry() {
        try {
            const res = await fetch('api/status');
            const data = await res.json();
            updateUI(data);
        } catch (err) {
            console.error('Failed to fetch status:', err);
        }
    }

    function updateUI(data) {
        const miniDot = document.getElementById('miniStatusIndicator');
        const miniText = document.getElementById('miniStatusText');
        const mobileDot = document.getElementById('mobileStatusIndicator');
        const mobileText = document.getElementById('mobileStatusText');
        const valStatus = document.getElementById('valStreamStatus');
        const screenDot = document.getElementById('screenDot');
        const screenText = document.getElementById('screenText');

        if (data.status === 'STREAMING') {
            if (miniDot) miniDot.className = 'status-indicator streaming';
            if (miniText) miniText.textContent = 'STREAMING LIVE';
            if (mobileDot) mobileDot.className = 'status-indicator streaming';
            if (mobileText) mobileText.textContent = 'LIVE';
            valStatus.textContent = 'ĐANG PHÁT LIVE';
            valStatus.style.color = 'var(--accent-emerald)';
            screenDot.className = 'pulse-dot active';
            screenText.textContent = 'LIVE BROADCASTING';
            btnStart.disabled = true;
            btnStop.disabled = false;
        } else if (data.status === 'RECONNECTING') {
            if (miniDot) miniDot.className = 'status-indicator';
            if (miniText) miniText.textContent = 'RECONNECTING';
            if (mobileDot) mobileDot.className = 'status-indicator';
            if (mobileText) mobileText.textContent = 'RECONNECT';
            valStatus.textContent = 'ĐANG KẾT NỐI LẠI';
            valStatus.style.color = 'var(--accent-amber)';
            btnStart.disabled = true;
            btnStop.disabled = false;
        } else {
            if (miniDot) miniDot.className = 'status-indicator stopped';
            if (miniText) miniText.textContent = 'STOPPED';
            if (mobileDot) mobileDot.className = 'status-indicator stopped';
            if (mobileText) mobileText.textContent = 'STOPPED';
            valStatus.textContent = 'ĐÃ DỪNG';
            valStatus.style.color = 'var(--accent-rose)';
            screenDot.className = 'pulse-dot';
            screenText.textContent = 'STREAM DISCONNECTED';
            btnStart.disabled = false;
            btnStop.disabled = true;
        }

        // Metrics
        document.getElementById('valUptime').textContent = `Thời gian phát: ${formatSeconds(data.uptime_seconds)}`;
        document.getElementById('valCpu').textContent = `${data.system.cpu_percent}%`;
        document.getElementById('barCpu').style.width = `${data.system.cpu_percent}%`;

        document.getElementById('valRam').textContent = `${data.system.ram_used_mb} / ${data.system.ram_total_mb} MB`;
        document.getElementById('barRam').style.width = `${data.system.ram_percent}%`;

        document.getElementById('valPlaylistCount').textContent = `${data.playlist_count} Video`;
        document.getElementById('valResolution').textContent = `Độ phân giải: ${data.resolution}`;

        // Screen Overlay Simulation
        document.getElementById('screenOverlayText').textContent = data.overlay_text || 'TIKTOK LIVE AUTOMATION';
        
        // Info panel
        document.getElementById('infoRtmpUrl').textContent = data.rtmp_url || 'Chưa cấu hình';
        document.getElementById('infoStreamKey').textContent = data.has_stream_key ? '•••••••••••• (Đã có)' : 'Chưa có Stream Key';
        document.getElementById('infoReconnectCount').textContent = `${data.reconnect_count} lần`;
        document.getElementById('infoLastError').textContent = data.last_error || 'Không có lỗi';
    }

    async function loadConfig() {
        try {
            const res = await fetch('api/config');
            const cfg = await res.json();
            document.getElementById('cfgRtmpUrl').value = cfg.rtmp_url || '';
            document.getElementById('cfgStreamKey').value = cfg.stream_key || '';
            document.getElementById('cfgResolution').value = cfg.resolution || '1080x1920';
            document.getElementById('cfgVideoBitrate').value = cfg.video_bitrate || '4000k';
            document.getElementById('cfgOverlayText').value = cfg.overlay_text || '';
            document.getElementById('cfgLoop').checked = cfg.loop !== false;
            document.getElementById('cfgAutoReconnect').checked = cfg.auto_reconnect !== false;
            document.getElementById('cfgShowClock').checked = cfg.show_clock !== false;
            // Load avatar overlay setting from overlay_config
            const overlayCfg = cfg.overlay_config || {};
            document.getElementById('cfgAvatarOverlay').checked = overlayCfg.avatar_overlay !== false;
        } catch (err) {
            console.error('Failed to load config:', err);
        }
    }

    // URL Modal handling
    const urlModal = document.getElementById('urlModal');
    const btnOpenUrl = document.getElementById('btnOpenUrlModal');
    const btnCancelUrl = document.getElementById('btnCancelUrl');
    const btnDownloadUrl = document.getElementById('btnDownloadUrl');

    if (btnOpenUrl) {
        btnOpenUrl.addEventListener('click', () => {
            urlModal.style.display = 'block';
            document.getElementById('inputVideoUrl').value = '';
        });
    }
    if (btnCancelUrl) {
        btnCancelUrl.addEventListener('click', () => { urlModal.style.display = 'none'; });
    }
    if (btnDownloadUrl) {
        btnDownloadUrl.addEventListener('click', async () => {
            const url = document.getElementById('inputVideoUrl').value.trim();
            if (!url) { showToast('Vui lòng nhập URL video!', 'rose'); return; }
            btnDownloadUrl.disabled = true;
            btnDownloadUrl.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang tải...';
            try {
                const res = await fetch('api/media/url', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url: url })
                });
                const data = await res.json();
                if (data.success) {
                    showToast(data.message, 'green');
                    setTimeout(() => loadMediaList(), 3000);
                    urlModal.style.display = 'none';
                } else {
                    showToast(data.detail || 'Lỗi tải video', 'rose');
                }
            } catch (err) {
                showToast('Lỗi kết nối: ' + err.message, 'rose');
            } finally {
                btnDownloadUrl.disabled = false;
                btnDownloadUrl.innerHTML = '<i class="fa-solid fa-download"></i> Tải Về';
            }
        });
    }

    // ---- Media Library with Thumbnails ----
    async function loadMediaList() {
        const container = document.getElementById('mediaListContainer');
        try {
            await loadPlaylist();
            const res = await fetch('api/media');
            const data = await res.json();

            if (!data.files || data.files.length === 0) {
                container.innerHTML = `<div class="empty-state">Chưa có video nào. Upload video hoặc thêm từ URL để bắt đầu.</div>`;
                return;
            }

            window._mediaFiles = data.files;
            renderMediaList();
        } catch (err) {
            container.innerHTML = `<div class="empty-state">Lỗi nạp danh sách video: ${err.message}</div>`;
        }
    }

    function renderMediaList() {
        const container = document.getElementById('mediaListContainer');
        const data = { files: window._mediaFiles || [] };

        if (!data.files || data.files.length === 0) {
            container.innerHTML = `<div class="empty-state">Chưa có video nào. Upload video hoặc thêm từ URL để bắt đầu.</div>`;
            return;
        }

        let html = `<div class="media-grid">`;
        for (const f of data.files) {
            const duration = f.duration ? formatDuration(f.duration) : '--:--';
            const thumb = f.thumb_url
                ? `<img src="${f.thumb_url}" class="media-thumb" loading="lazy">`
                : `<div class="media-thumb-placeholder"><i class="fa-solid fa-film"></i></div>`;
            const inPlaylist = window._currentPlaylist && window._currentPlaylist.includes(f.name);

            html += `
            <div class="media-card" data-filename="${f.name}">
                ${thumb}
                <div class="media-info">
                    <span class="media-name" title="${f.name}">${f.name}</span>
                    <span class="media-meta">${f.size_mb} MB • ${duration}</span>
                </div>
                <div class="media-actions-row">
                    <label class="checkbox-label media-checkbox" title="Chọn để thêm vào playlist">
                        <input type="checkbox" onchange="toggleMediaInPlaylist('${f.name}')" ${inPlaylist ? 'checked' : ''}>
                    </label>
                    <button class="btn-icon btn-amber" onclick="convertMedia('${f.name}')" title="Convert to H.264">
                        <i class="fa-solid fa-gears"></i>
                    </button>
                    <button class="btn-icon btn-rose" onclick="deleteMedia('${f.name}')" title="Xóa">
                        <i class="fa-solid fa-trash"></i>
                    </button>
                </div>
            </div>`;
        }
        html += `</div>`;

        // Playlist order display with up/down reorder buttons
        html += `<div class="playlist-section">
            <div class="playlist-header">
                <h4><i class="fa-solid fa-list-order"></i> Thứ Tự Phát Playlist</h4>
                <button class="btn btn-emerald btn-sm" onclick="savePlaylist()">
                    <i class="fa-solid fa-save"></i> Lưu Playlist
                </button>
            </div>
            <ul class="playlist-list">`;

        if (window._currentPlaylist && window._currentPlaylist.length) {
            window._currentPlaylist.forEach((name, idx) => {
                const fileInfo = data.files.find(f => f.name === name);
                const dur = fileInfo && fileInfo.duration ? formatDuration(fileInfo.duration) : '';
                const canUp = idx > 0;
                const canDown = idx < window._currentPlaylist.length - 1;
                html += `<li class="playlist-item">
                    <span class="playlist-order">${idx + 1}</span>
                    <span class="playlist-name">${name}</span>
                    <span class="playlist-dur">${dur}</span>
                    <div class="playlist-move-btns">
                        <button class="btn-icon btn-cyan btn-xs" onclick="movePlaylistUp(${idx})" title="Lên" ${canUp ? '' : 'disabled'}><i class="fa-solid fa-up-long"></i></button>
                        <button class="btn-icon btn-cyan btn-xs" onclick="movePlaylistDown(${idx})" title="Xuống" ${canDown ? '' : 'disabled'}><i class="fa-solid fa-down-long"></i></button>
                    </div>
                </li>`;
            });
        } else {
            html += `<li class="empty-state-sm">Chưa có video trong playlist. Chọn checkbox để thêm.</li>`;
        }
        html += `</ul></div>`;

        container.innerHTML = html;
    }

    function formatDuration(seconds) {
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m}:${String(s).padStart(2, '0')}`;
    }

    window.deleteMedia = async function(fname) {
        if (!confirm(`Bạn có chắc muốn xóa video ${fname}?`)) return;
        try {
            const res = await fetch(`api/media/${encodeURIComponent(fname)}`, { method: 'DELETE' });
            if (!res.ok) throw new Error(`Xóa thất bại (HTTP ${res.status})`);
            await res.json();
            showToast(`Đã xóa ${fname}`, 'emerald');
            await loadMediaList();
        } catch (err) {
            showToast(err.message, 'rose');
        }
    };

    window.convertMedia = async function(fname) {
        showToast(`Đang convert ${fname}...`, 'amber');
        try {
            const res = await fetch(`api/media/${encodeURIComponent(fname)}/convert`, { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                showToast(data.already_compatible ? 'Đã đúng định dạng' : 'Convert hoàn tất!', 'emerald');
            } else {
                showToast(data.error || 'Convert lỗi', 'rose');
            }
        } catch (err) {
            showToast(err.message, 'rose');
        }
    };

    // Track current playlist and media files
    window._currentPlaylist = [];
    window._mediaFiles = [];

    // Load saved playlist
    async function loadPlaylist() {
        try {
            const res = await fetch('api/media/playlist');
            const data = await res.json();
            window._currentPlaylist = data.playlist || [];
        } catch (err) {
            window._currentPlaylist = [];
        }
    }

    // Toggle video in playlist (updates local state then re-renders)
    window.toggleMediaInPlaylist = function(fname) {
        const idx = window._currentPlaylist.indexOf(fname);
        if (idx >= 0) {
            window._currentPlaylist.splice(idx, 1);
        } else {
            window._currentPlaylist.push(fname);
        }
        renderMediaList();
    }

    window.movePlaylistUp = function(idx) {
        if (idx <= 0) return;
        const tmp = window._currentPlaylist[idx];
        window._currentPlaylist[idx] = window._currentPlaylist[idx - 1];
        window._currentPlaylist[idx - 1] = tmp;
        renderMediaList();
    }

    window.movePlaylistDown = function(idx) {
        if (idx >= window._currentPlaylist.length - 1) return;
        const tmp = window._currentPlaylist[idx];
        window._currentPlaylist[idx] = window._currentPlaylist[idx + 1];
        window._currentPlaylist[idx + 1] = tmp;
        renderMediaList();
    }

    window.savePlaylist = async function() {
        try {
            const res = await fetch('api/media/playlist', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({playlist: window._currentPlaylist})
            });
            const data = await res.json();
            if (data.success) {
                showToast(`Đã lưu ${window._currentPlaylist.length} video. Dừng stream và bật lại để áp dụng!`, 'amber');
            }
        } catch (err) {
            showToast(err.message, 'rose');
        }
    }

    async function loadLogs() {
        const terminal = document.getElementById('logTerminal');
        try {
            const res = await fetch('api/logs');
            const data = await res.json();
            terminal.innerHTML = data.logs.map(line => {
                let cls = 'log-line';
                if (line.includes('ERROR') || line.includes('Failed')) cls += ' error';
                if (line.includes('INFO')) cls += ' info';
                return `<div class="${cls}">${escapeHtml(line)}</div>`;
            }).join('');
            terminal.scrollTop = terminal.scrollHeight;
        } catch (err) {
            terminal.innerHTML = `<div class="log-line error">Lỗi tải nhật ký: ${err.message}</div>`;
        }
    }

    document.getElementById('btnRefreshLogs')?.addEventListener('click', loadLogs);

    // ===== HELPERS =====
    function formatSeconds(secs) {
        if (!secs) return '00:00:00';
        const h = Math.floor(secs / 3600).toString().padStart(2, '0');
        const m = Math.floor((secs % 3600) / 60).toString().padStart(2, '0');
        const s = (secs % 60).toString().padStart(2, '0');
        return `${h}:${m}:${s}`;
    }

    function escapeHtml(str) {
        return str.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    }

    let activeToast = null;
    function showToast(msg, type = 'emerald') {
        const container = document.getElementById('toastContainer');
        if (activeToast && activeToast.parentNode) {
            activeToast.textContent = msg;
            activeToast.className = `toast toast-${type}`;
            clearTimeout(activeToast._hideTimer);
            activeToast._hideTimer = setTimeout(() => activeToast.remove(), 4000);
            return activeToast;
        }
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = msg;
        container.appendChild(toast);
        toast._hideTimer = setTimeout(() => toast.remove(), 4000);
        activeToast = toast;
        return toast;
    }

    function clearActiveToast() {
        if (activeToast) {
            clearTimeout(activeToast._hideTimer);
            activeToast.remove();
            activeToast = null;
        }
    }

    // Preview image refresh (when streaming)
    setInterval(() => {
        const img = document.getElementById('previewImg');
        const screenDot = document.getElementById('screenDot');
        if (img && screenDot && screenDot.classList.contains('active')) {
            img.style.display = 'block';
            img.src = 'api/preview.jpg?t=' + Date.now();
            const placeholder = document.getElementById('screenPlaceholder');
            if (placeholder) placeholder.style.display = 'none';
        } else if (img) {
            img.style.display = 'none';
            const placeholder = document.getElementById('screenPlaceholder');
            if (placeholder) placeholder.style.display = 'block';
        }
    }, 500);

    // Initial Fetch & Start Polling
    fetchTelemetry();
    loadConfig();
    setInterval(fetchTelemetry, 2000);
});
