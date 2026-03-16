/**
 * Collects hardware and browser signals and injects them as hidden
 * fields into the voter verification form before submission.
 *
 * Signals collected:
 *   fp_canvas     - Canvas 2D rendering hash (GPU + font signature)
 *   fp_wgl_vendor - WebGL GPU vendor string
 *   fp_wgl_render - WebGL GPU renderer string
 *   fp_screen     - Screen resolution, color depth, device pixel ratio
 *   fp_tz         - Browser timezone
 *   fp_lang       - navigator.language
 *   fp_platform   - navigator.platform
 *   fp_ua_hash    - SHA-256 of user agent string
 *
 * The server combines all of these into a composite SHA-256 hash.
 *
 * Privacy note: no data is sent to any third party. All values are
 * hashed or normalized before storage. Raw user-agent is never stored.
 */

'use strict';

// -----------------------------------------------------------------------
// SHA-256 (pure JS, no external deps)
// -----------------------------------------------------------------------
async function sha256(message) {
    const msgBuffer = new TextEncoder().encode(message);
    const hashBuffer = await crypto.subtle.digest('SHA-256', msgBuffer);
    const hashArray = Array.from(new Uint8Array(hashBuffer));
    return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
}

// -----------------------------------------------------------------------
// Canvas fingerprint
// Draws text + shapes — rendering differences reveal GPU/font stack
// -----------------------------------------------------------------------
function getCanvasHash_raw() {
    try {
        const canvas = document.createElement('canvas');
        canvas.width = 200;
        canvas.height = 50;
        const ctx = canvas.getContext('2d');
        if (!ctx) return '';

        ctx.textBaseline = 'top';
        ctx.font = '14px Arial';
        ctx.fillStyle = '#f60';
        ctx.fillRect(10, 1, 62, 20);
        ctx.fillStyle = '#069';
        ctx.fillText('UDS-Election 🗳', 2, 15);
        ctx.fillStyle = 'rgba(102,204,0,0.7)';
        ctx.fillText('UDS-Election 🗳', 4, 17);

        return canvas.toDataURL();
    } catch (e) {
        return '';
    }
}

// -----------------------------------------------------------------------
// WebGL signals
// -----------------------------------------------------------------------
function getWebGLInfo() {
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
        if (!gl) return { vendor: '', renderer: '' };

        const ext = gl.getExtension('WEBGL_debug_renderer_info');
        return {
            vendor: ext ? (gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) || '') : '',
            renderer: ext ? (gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) || '') : '',
        };
    } catch (e) {
        return { vendor: '', renderer: '' };
    }
}

// -----------------------------------------------------------------------
// Screen info
// -----------------------------------------------------------------------
function getScreenInfo() {
    return [
        screen.width,
        screen.height,
        screen.colorDepth,
        window.devicePixelRatio || 1
    ].join('x');
}

// -----------------------------------------------------------------------
// Collect all signals, hash them, inject into form
// -----------------------------------------------------------------------
async function injectFingerprint(form) {
    const canvasRaw = getCanvasHash_raw();
    const wgl = getWebGLInfo();
    const ua_hash = await sha256(navigator.userAgent || '');
    const canvas_hash = canvasRaw ? await sha256(canvasRaw) : '';

    const fields = {
        fp_canvas: canvas_hash,
        fp_wgl_vendor: (wgl.vendor || '').substring(0, 128),
        fp_wgl_render: (wgl.renderer || '').substring(0, 128),
        fp_screen: getScreenInfo(),
        fp_tz: (Intl.DateTimeFormat().resolvedOptions().timeZone || '').substring(0, 64),
        fp_lang: (navigator.language || '').substring(0, 32),
        fp_platform: (navigator.platform || '').substring(0, 64),
        fp_ua_hash: ua_hash,
    };

    for (const [name, value] of Object.entries(fields)) {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = name;
        input.value = value;
        form.appendChild(input);
    }
}

// -----------------------------------------------------------------------
// Attach to the verification form on DOMContentLoaded
// -----------------------------------------------------------------------
document.addEventListener('DOMContentLoaded', function () {
    const form = document.getElementById('verificationForm');
    if (!form) return;

    form.addEventListener('submit', async function (e) {
        e.preventDefault();
        try {
            await injectFingerprint(form);
        } catch (err) {
            // Never block the user — if fingerprinting fails, submit anyway
            console.warn('Fingerprint collection failed (non-fatal):', err);
        }
        form.submit();
    });
});