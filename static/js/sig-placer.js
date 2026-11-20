/*
 * SigPlacer – place a signature on a PDF page.
 *   - the page is rendered with pdf.js, so the preview has the exact page geometry
 *   - drag the signature to move it, drag any corner to resize (aspect ratio locked)
 *   - transparent PNGs are shown as-is over the page (no white box)
 *   - getPlacement() returns fractions of the displayed page, which the server uses
 *     to draw the signature in exactly the same spot in the final PDF
 *
 * Usage:
 *   const placer = SigPlacer.mount(containerEl, { pdfUrl, sigUrl });
 *   placer.getPlacement()  ->  { page_index, rel_x, rel_y, rel_w, rel_h }
 */
(function () {
  if (window.SigPlacer) return;

  var PDFJS = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js';
  var PDFJS_WORKER = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';

  function loadPdfJs() {
    if (window.pdfjsLib) return Promise.resolve(window.pdfjsLib);
    if (window.__pdfjsLoading) return window.__pdfjsLoading;
    window.__pdfjsLoading = new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = PDFJS;
      s.onload = function () {
        window.pdfjsLib.GlobalWorkerOptions.workerSrc = PDFJS_WORKER;
        resolve(window.pdfjsLib);
      };
      s.onerror = function () { reject(new Error('Could not load the PDF viewer.')); };
      document.head.appendChild(s);
    });
    return window.__pdfjsLoading;
  }

  function injectCss() {
    if (document.getElementById('sgp-css')) return;
    var css = document.createElement('style');
    css.id = 'sgp-css';
    css.textContent = [
      '.sgp{display:flex;flex-direction:column;height:100%;min-height:380px;}',
      '.sgp-toolbar{display:flex;align-items:center;gap:6px;padding:6px 8px;background:#f9fafb;border-bottom:1px solid #e5e7eb;font-size:12px;color:#374151;flex-wrap:wrap;}',
      '.sgp-btn{border-radius:999px;border:1px solid #d1d5db;padding:3px 9px;background:#fff;cursor:pointer;font-size:12px;}',
      '.sgp-btn:hover{background:#f3f4f6;}',
      '.sgp-hint{margin-left:auto;color:#6b7280;font-size:11px;}',
      '.sgp-stage{flex:1;overflow:auto;background:#111827;padding:12px;display:flex;justify-content:center;align-items:flex-start;}',
      '.sgp-page-wrap{position:relative;box-shadow:0 4px 18px rgba(0,0,0,.4);background:#fff;}',
      '.sgp-page-wrap canvas{display:block;}',
      '.sgp-box{position:absolute;cursor:move;touch-action:none;outline:1.5px dashed #16a34a;outline-offset:0;user-select:none;}',
      '.sgp-box img{width:100%;height:100%;display:block;pointer-events:none;-webkit-user-drag:none;}',
      '.sgp-h{position:absolute;width:12px;height:12px;background:#fff;border:2px solid #16a34a;border-radius:3px;touch-action:none;}',
      '.sgp-h.nw{left:-7px;top:-7px;cursor:nwse-resize}.sgp-h.ne{right:-7px;top:-7px;cursor:nesw-resize}',
      '.sgp-h.sw{left:-7px;bottom:-7px;cursor:nesw-resize}.sgp-h.se{right:-7px;bottom:-7px;cursor:nwse-resize}',
      '.sgp-msg{color:#e5e7eb;font-size:13px;padding:30px;text-align:center;}'
    ].join('');
    document.head.appendChild(css);
  }

  function mount(container, opts) {
    injectCss();
    container.innerHTML =
      '<div class="sgp">' +
      '  <div class="sgp-toolbar">' +
      '    <button type="button" class="sgp-btn" data-act="prev" title="Previous page">&#9664;</button>' +
      '    <span>Page <b data-el="num">1</b> / <b data-el="count">–</b></span>' +
      '    <button type="button" class="sgp-btn" data-act="next" title="Next page">&#9654;</button>' +
      '    <button type="button" class="sgp-btn" data-act="smaller" title="Smaller">&minus;</button>' +
      '    <button type="button" class="sgp-btn" data-act="bigger" title="Bigger">+</button>' +
      '    <span class="sgp-hint">Drag to move &middot; drag a corner to resize</span>' +
      '  </div>' +
      '  <div class="sgp-stage"><div class="sgp-msg">Loading document…</div></div>' +
      '</div>';

    var stage = container.querySelector('.sgp-stage');
    var numEl = container.querySelector('[data-el="num"]');
    var countEl = container.querySelector('[data-el="count"]');

    var pdf = null, pageNum = 1, wrap = null, canvas = null, box = null, img = null;
    var aspect = 0.35;                 // height / width of the signature image
    var rel = { x: 0.58, y: 0.74, w: 0.25 }; // top-left + width, fractions of the page
    var pageW = 0, pageH = 0;
    var renderTask = null;

    function clampRel() {
      var hRel = (rel.w * pageW * aspect) / pageH;
      if (hRel > 0.9) { hRel = 0.9; rel.w = (hRel * pageH) / (pageW * aspect); }
      rel.w = Math.max(0.03, Math.min(0.9, rel.w));
      hRel = (rel.w * pageW * aspect) / pageH;
      rel.x = Math.max(0, Math.min(1 - rel.w, rel.x));
      rel.y = Math.max(0, Math.min(1 - hRel, rel.y));
    }

    function layoutBox() {
      if (!box || !pageW) return;
      clampRel();
      var w = rel.w * pageW;
      box.style.left = rel.x * pageW + 'px';
      box.style.top = rel.y * pageH + 'px';
      box.style.width = w + 'px';
      box.style.height = w * aspect + 'px';
    }

    function buildBox() {
      box = document.createElement('div');
      box.className = 'sgp-box';
      img = document.createElement('img');
      img.alt = 'Signature';
      img.draggable = false;
      img.onload = function () {
        if (img.naturalWidth) aspect = img.naturalHeight / img.naturalWidth;
        layoutBox();
      };
      img.src = opts.sigUrl;
      box.appendChild(img);
      ['nw', 'ne', 'sw', 'se'].forEach(function (c) {
        var h = document.createElement('span');
        h.className = 'sgp-h ' + c;
        h.dataset.corner = c;
        box.appendChild(h);
      });
      wireDrag();
    }

    function renderPage() {
      if (!pdf) return;
      pdf.getPage(pageNum).then(function (page) {
        var base = page.getViewport({ scale: 1 });
        var avail = Math.max(240, stage.clientWidth - 24);
        var scale = avail / base.width;
        var vp = page.getViewport({ scale: scale });
        var dpr = window.devicePixelRatio || 1;

        if (!wrap) {
          stage.innerHTML = '';
          wrap = document.createElement('div');
          wrap.className = 'sgp-page-wrap';
          canvas = document.createElement('canvas');
          wrap.appendChild(canvas);
          stage.appendChild(wrap);
          buildBox();
          wrap.appendChild(box);
        }
        pageW = vp.width; pageH = vp.height;
        canvas.width = Math.floor(vp.width * dpr);
        canvas.height = Math.floor(vp.height * dpr);
        canvas.style.width = vp.width + 'px';
        canvas.style.height = vp.height + 'px';
        wrap.style.width = vp.width + 'px';
        wrap.style.height = vp.height + 'px';
        if (renderTask) { try { renderTask.cancel(); } catch (e) {} }
        renderTask = page.render({
          canvasContext: canvas.getContext('2d'),
          viewport: vp,
          transform: dpr !== 1 ? [dpr, 0, 0, dpr, 0, 0] : null
        });
        numEl.textContent = pageNum;
        layoutBox();
      });
    }

    function wireDrag() {
      var mode = null, start = null;
      box.addEventListener('pointerdown', function (e) {
        e.preventDefault();
        box.setPointerCapture(e.pointerId);
        mode = e.target.dataset.corner || 'move';
        start = { x: e.clientX, y: e.clientY, rx: rel.x, ry: rel.y, rw: rel.w };
      });
      box.addEventListener('pointermove', function (e) {
        if (!mode) return;
        var dx = (e.clientX - start.x) / pageW;
        var dy = (e.clientY - start.y) / pageH;
        if (mode === 'move') {
          rel.x = start.rx + dx;
          rel.y = start.ry + dy;
        } else {
          // keep the opposite corner fixed; width follows the pointer, height follows the aspect ratio
          var sign = (mode === 'ne' || mode === 'se') ? 1 : -1;
          var dyAsW = (dy * pageH) / (aspect * pageW);
          var vSign = (mode === 'sw' || mode === 'se') ? 1 : -1;
          var dw = Math.abs(dx) > Math.abs(dyAsW) ? sign * dx : vSign * dyAsW;
          var newW = Math.max(0.03, Math.min(0.9, start.rw + dw));
          var oldH = (start.rw * pageW * aspect) / pageH;
          var newH = (newW * pageW * aspect) / pageH;
          rel.w = newW;
          rel.x = sign === 1 ? start.rx : start.rx + (start.rw - newW);
          rel.y = vSign === 1 ? start.ry : start.ry + (oldH - newH);
        }
        layoutBox();
      });
      function end() { mode = null; }
      box.addEventListener('pointerup', end);
      box.addEventListener('pointercancel', end);
    }

    container.querySelector('[data-act="prev"]').addEventListener('click', function () {
      if (pdf && pageNum > 1) { pageNum--; renderPage(); }
    });
    container.querySelector('[data-act="next"]').addEventListener('click', function () {
      if (pdf && pageNum < pdf.numPages) { pageNum++; renderPage(); }
    });
    container.querySelector('[data-act="smaller"]').addEventListener('click', function () {
      var cx = rel.x + rel.w / 2; rel.w = rel.w * 0.9; rel.x = cx - rel.w / 2; layoutBox();
    });
    container.querySelector('[data-act="bigger"]').addEventListener('click', function () {
      var cx = rel.x + rel.w / 2; rel.w = rel.w * 1.1; rel.x = cx - rel.w / 2; layoutBox();
    });

    var resizeT = null;
    function onResize() { clearTimeout(resizeT); resizeT = setTimeout(renderPage, 150); }
    window.addEventListener('resize', onResize);

    loadPdfJs()
      .then(function (lib) { return lib.getDocument({ url: opts.pdfUrl, withCredentials: true }).promise; })
      .then(function (doc) {
        pdf = doc;
        countEl.textContent = doc.numPages;
        if (opts.startPage && opts.startPage <= doc.numPages) pageNum = opts.startPage;
        renderPage();
      })
      .catch(function (err) {
        stage.innerHTML = '<div class="sgp-msg">Unable to load the document preview. ' + (err && err.message ? err.message : '') + '</div>';
      });

    return {
      getPlacement: function () {
        clampRel();
        return {
          page_index: pageNum - 1,
          rel_x: +rel.x.toFixed(5),
          rel_y: +rel.y.toFixed(5),
          rel_w: +rel.w.toFixed(5),
          rel_h: +((rel.w * pageW * aspect) / pageH).toFixed(5)
        };
      },
      isReady: function () { return !!(pdf && pageW); },
      destroy: function () { window.removeEventListener('resize', onResize); container.innerHTML = ''; }
    };
  }

  window.SigPlacer = { mount: mount };
})();
