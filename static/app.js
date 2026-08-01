document.addEventListener('DOMContentLoaded', () => {
    // Navigation Tabs (Desktop & Mobile)
    const navItems = document.querySelectorAll('.nav-item, .mobile-nav-item');
    const tabPages = document.querySelectorAll('.tab-page');
    const pageTitleText = document.getElementById('pageTitleText');

    const tabTitles = {
        'dashboard': 'Bảng Điều Khiển Stream',
        'config': 'Cấu Hình Luồng Stream',
        'media': 'Quản Lý Video Playlist',
        'logs': 'Nhật Ký FFmpeg Core',
        'ai': 'AI Trợ Lý Live',
        'live': 'Live Comment Monitoring'
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
            if (targetTab === 'media') { loadMediaList().then(() => setTimeout(loadPlaylist, 200)); }
            if (targetTab === 'logs') loadLogs();
            if (targetTab === 'ai') loadAIConfig();
            if (targetTab === 'live') loadLiveConfig();

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
        const payload = {
            rtmp_url: document.getElementById('cfgRtmpUrl').value,
            stream_key: document.getElementById('cfgStreamKey').value,
            resolution: document.getElementById('cfgResolution').value,
            video_bitrate: document.getElementById('cfgVideoBitrate').value,
            overlay_text: document.getElementById('cfgOverlayText').value,
            loop: document.getElementById('cfgLoop').checked,
            auto_reconnect: document.getElementById('cfgAutoReconnect').checked,
            show_clock: document.getElementById('cfgShowClock').checked
        };

        // Also save tiktok username if filled
        const liveUser = document.getElementById('liveUsername');
        if (liveUser && liveUser.value.trim()) {
            payload.tiktok_username = liveUser.value.trim().replace('@', '');
        }

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

    // File Upload
    let uploadActive = false;
    const fileUploadInput = document.getElementById('fileUploadInput');
    fileUploadInput.addEventListener('change', (e) => {
        const file = e.target.files[0];
        if (!file) return;

        uploadActive = true;
        const formData = new FormData();
        formData.append('file', file);

        let progressToast = showToast(`Đang tải lên ${file.name}... 0%`, 'cyan');

        const xhr = new XMLHttpRequest();
        xhr.open('POST', 'api/media/upload');

        xhr.upload.onprogress = (event) => {
            if (event.lengthComputable) {
                const percent = Math.round((event.loaded / event.total) * 100);
                progressToast = showToast(`Đang tải lên ${file.name}: ${percent}%`, 'cyan');
            }
        };

        xhr.onload = () => {
            uploadActive = false;
            fileUploadInput.value = '';
            clearActiveToast();
            if (xhr.status === 200) {
                const data = JSON.parse(xhr.responseText);
                if (data.converted) {
                    showToast(`Tải lên ${file.name} thành công! Đã convert H.264`, 'emerald');
                } else {
                    showToast(`Tải lên ${file.name} thành công! Đang convert nền...`, 'emerald');
                }
                loadMediaList();
                setTimeout(() => { loadMediaList(); if (typeof loadPlaylist === 'function') loadPlaylist(); }, 5000);
            } else if (xhr.status === 413) {
                showToast(`File ${file.name} quá lớn (max 600MB)!`, 'rose');
            } else {
                showToast(`Lỗi tải lên (${xhr.status}): ${xhr.responseText.slice(0,100)}`, 'rose');
            }
        };

        xhr.onerror = () => {
            uploadActive = false;
            clearActiveToast();
            showToast(`Lỗi kết nối khi tải ${file.name}`, 'rose');
        };

        xhr.send(formData);
    });

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

    async function loadMediaList() {
        const container = document.getElementById('mediaListContainer');
        try {
            const res = await fetch('api/media');
            const data = await res.json();

            if (!data.files || data.files.length === 0) {
                container.innerHTML = `<div class="empty-state">Chưa có video nào. Hệ thống sẽ tự động phát Video Test Pattern mặc định nếu bạn nhấn Start Live.</div>`;
                return;
            }

            container.innerHTML = `<div class="playlist-controls" style="margin-bottom:12px;display:flex;gap:8px;align-items:center;">
                <button class="btn btn-emerald btn-sm" onclick="savePlaylist()">
                    <i class="fa-solid fa-save"></i> Lưu Playlist
                </button>
                <span class="text-muted" style="font-size:12px;" id="playlistCount">0 video đã chọn</span>
            </div>` + data.files.map(f => `
                <div class="media-item">
                    <div class="media-name" onclick="togglePlaylist('${f.name}')" style="cursor:pointer;">
                        <i class="fa-solid fa-file-video text-emerald"></i>
                        <input type="checkbox" class="playlist-checkbox" value="${f.name}" style="margin-left:8px;" onchange="togglePlaylist('${f.name}')">
                        <span>${f.name}</span>
                        <small class="text-muted">(${f.size_mb} MB)</small>
                    </div>
                    <div class="media-actions">
                        <button class="btn btn-amber btn-sm" onclick="convertMedia('${f.name}')" title="Convert to H.264">
                            <i class="fa-solid fa-gears"></i>
                        </button>
                        <button class="btn btn-rose btn-sm" onclick="deleteMedia('${f.name}')">
                            <i class="fa-solid fa-trash"></i> Xóa
                        </button>
                    </div>
                </div>
            `).join('');
            updatePlaylistCount();
            // Load saved playlist after rendering
            setTimeout(loadPlaylist, 100);
        } catch (err) {
            container.innerHTML = `<div class="empty-state">Lỗi nạp danh sách video: ${err.message}</div>`;
        }
    }

    window.deleteMedia = async function(fname) {
        if (!confirm(`Bạn có chắc muốn xóa video ${fname}?`)) return;
        try {
            await fetch(`api/media/${encodeURIComponent(fname)}`, { method: 'DELETE' });
            showToast(`Đã xóa ${fname}`, 'emerald');
            loadMediaList();
        } catch (err) {
            showToast(err.message, 'rose');
        }
    };

    window.convertMedia = async function(fname) {
        showToast(`Đang convert ${fname} → H.264...`, 'amber');
        try {
            const res = await fetch(`api/media/${encodeURIComponent(fname)}/convert`, { method: 'POST' });
            const data = await res.json();
            if (data.success) {
                showToast(data.already_h264 ? 'Đã là H.264' : 'Convert hoàn tất!', 'emerald');
            } else {
                showToast(data.error || 'Convert lỗi', 'rose');
            }
        } catch (err) {
            showToast(err.message, 'rose');
        }
    };

    window.togglePlaylist = function(fname) {
        const cb = document.querySelector(`input[value="${fname}"]`);
        if (cb) cb.checked = !cb.checked;
        updatePlaylistCount();
    };

    function updatePlaylistCount() {
        const checked = document.querySelectorAll('.playlist-checkbox:checked');
        const countEl = document.getElementById('playlistCount');
        if (countEl) countEl.textContent = `${checked.length} video đã chọn`;
    }

    window.savePlaylist = async function() {
        const checked = document.querySelectorAll('.playlist-checkbox:checked');
        const playlist = Array.from(checked).map(cb => cb.value);
        try {
            const res = await fetch('api/media/playlist', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({playlist: playlist})
            });
            const data = await res.json();
            if (data.success) {
                showToast(`Đã lưu ${playlist.length} video. Dừng stream và bật lại để áp dụng!`, 'amber');
            }
        } catch (err) {
            showToast(err.message, 'rose');
        }
    };

    // Load saved playlist on media tab
    async function loadPlaylist() {
        try {
            const res = await fetch('api/media/playlist');
            const data = await res.json();
            if (data.playlist) {
                data.playlist.forEach(fname => {
                    const cb = document.querySelector(`input[value="${fname}"]`);
                    if (cb) cb.checked = true;
                });
                updatePlaylistCount();
            }
        } catch (err) {}
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

    // ===== AI ENGINE FUNCTIONS =====

    // AI Config Form
    const aiConfigForm = document.getElementById('aiConfigForm');
    if (aiConfigForm) {
        aiConfigForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            const payload = {
                enabled: document.getElementById('aiEnabled').checked,
                api_key: document.getElementById('aiApiKey').value,
                model: document.getElementById('aiModel').value,
                persona: document.getElementById('aiPersona').value
            };

            try {
                const res = await fetch('api/ai/configure', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                const data = await res.json();
                if (!res.ok) throw new Error(data.detail || 'Lỗi cấu hình AI');
                showToast('AI engine đã được cấu hình!', 'emerald');
                fetchTelemetry();
            } catch (err) {
                showToast(err.message, 'rose');
            }
        });
    }

    async function loadAIConfig() {
        try {
            const res = await fetch('api/ai/status');
            const data = await res.json();
            document.getElementById('aiEnabled').checked = data.enabled;
            document.getElementById('aiModel').value = data.model;
            // Note: API key is not returned for security
        } catch (err) {
            console.error('Failed to load AI config:', err);
        }

        // Load AI response cache
        try {
            const res = await fetch('api/ai/responses');
            const data = await res.json();
            const logEl = document.getElementById('aiResponseLog');
            if (logEl) {
                if (!data.responses || data.responses.length === 0) {
                    logEl.innerHTML = '<div class="log-line text-muted">Chưa có phản hồi AI nào.</div>';
                } else {
                    logEl.innerHTML = data.responses.map(r => `
                        <div class="log-line">
                            <span class="text-cyan">[${r.timestamp}]</span>
                            <span class="text-emerald">${escapeHtml(r.username)}:</span> ${escapeHtml(r.comment)}
                            <br><span class="text-amber">🤖 AI:</span> ${escapeHtml(r.response)}
                        </div>
                    `).join('');
                    logEl.scrollTop = logEl.scrollHeight;
                }
            }
        } catch (err) {
            console.error('Failed to load AI responses:', err);
        }
    }

    document.getElementById('btnClearAICache')?.addEventListener('click', async () => {
        if (!confirm('Xóa toàn bộ lịch sử phản hồi AI?')) return;
        try {
            await fetch('api/ai/cache', { method: 'DELETE' });
            showToast('Đã xóa lịch sử AI', 'emerald');
            loadAIConfig();
        } catch (err) {
            showToast(err.message, 'rose');
        }
    });

    // ===== LIVE CLIENT FUNCTIONS =====

    const liveConnectForm = document.getElementById('liveConnectForm');
    if (liveConnectForm) {
        liveConnectForm.addEventListener('submit', async (e) => {
            e.preventDefault();
            let username = document.getElementById('liveUsername').value.trim();
            if (username.startsWith('@')) username = username.slice(1);
            
            if (!username) {
                showToast('Nhập username TikTok!', 'rose');
                return;
            }

            const btnConnect = document.getElementById('btnLiveConnect');
            btnConnect.disabled = true;
            btnConnect.innerHTML = '<i class="fa-solid fa-spinner fa-spin"></i> Đang kết nối...';

            try {
                const res = await fetch('api/live/connect', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username: username })
                });
                const data = await res.json();
                showToast(data.message || 'Kết nối live thành công!', data.success ? 'emerald' : 'rose');
                if (data.success) {
                    document.getElementById('liveStatsGrid').style.display = 'block';
                }
                fetchTelemetry();
            } catch (err) {
                showToast(err.message, 'rose');
            } finally {
                btnConnect.disabled = false;
                btnConnect.innerHTML = '<i class="fa-solid fa-plug-waveform"></i> KẾT NỐI TIKTOK LIVE';
            }
        });
    }

    async function loadLiveConfig() {
        try {
            const res = await fetch('api/live/status');
            const data = await res.json();
            
            const statsGrid = document.getElementById('liveStatsGrid');
            if (statsGrid) {
                statsGrid.style.display = data.connected ? 'grid' : 'none';
            }

            // Load comments
            const res2 = await fetch('api/live/comments?count=50');
            const data2 = await res2.json();
            const feed = document.getElementById('liveCommentFeed');
            if (feed) {
                if (!data2.comments || data2.comments.length === 0) {
                    feed.innerHTML = '<div class="comment-item text-muted">Chưa có bình luận nào...</div>';
                } else {
                    feed.innerHTML = data2.comments.map(c => `
                        <div class="comment-item">
                            <span class="comment-time">${c.timestamp}</span>
                            <span class="comment-user">${escapeHtml(c.user)}:</span>
                            <span class="comment-text">${escapeHtml(c.comment)}</span>
                        </div>
                    `).join('');
                    feed.scrollTop = feed.scrollHeight;
                }
            }
        } catch (err) {
            console.error('Failed to load live data:', err);
        }
    }

    document.getElementById('btnClearComments')?.addEventListener('click', async () => {
        const feed = document.getElementById('liveCommentFeed');
        if (feed) {
            feed.innerHTML = '<div class="comment-item text-muted">Đã xóa danh sách bình luận.</div>';
        }
    });

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

    // Update Simulated Clock
    setInterval(() => {
        const now = new Date();
        const timeStr = now.toTimeString().split(' ')[0];
        const clockEl = document.getElementById('screenClock');
        if (clockEl) clockEl.textContent = timeStr;
    }, 1000);

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
