function initGraffiti(canvasId, dataInputId, onFirstDraw) {
  const canvas    = document.getElementById(canvasId);
  const dataInput = document.getElementById(dataInputId);
  const ctx       = canvas.getContext('2d');

  let isDrawing = false;
  let hasDrawn  = false;
  let mode      = 'pen';
  let penColor  = '#1a1a1a';
  let penSize   = 4;
  let history   = [];

  function applyStyle() {
    ctx.lineJoin = 'round';
    ctx.lineCap  = 'round';
    if (mode === 'eraser') {
      ctx.strokeStyle = '#ffffff';
      ctx.lineWidth   = penSize * 4;
    } else {
      ctx.strokeStyle = penColor;
      ctx.lineWidth   = penSize;
    }
  }

  function initCanvas() {
    canvas.width  = canvas.offsetWidth || canvas.parentElement.offsetWidth || 560;
    canvas.height = 280;
    ctx.fillStyle = '#ffffff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    applyStyle();
  }

  function getPos(e) {
    const r   = canvas.getBoundingClientRect();
    const src = e.touches ? e.touches[0] : e;
    return {
      x: (src.clientX - r.left) * canvas.width  / r.width,
      y: (src.clientY - r.top)  * canvas.height / r.height
    };
  }

  function saveHistory() {
    history.push(ctx.getImageData(0, 0, canvas.width, canvas.height));
    if (history.length > 30) history.shift();
  }

  canvas.addEventListener('mousedown', e => {
    saveHistory();
    isDrawing = true;
    if (!hasDrawn && onFirstDraw) { hasDrawn = true; onFirstDraw(); }
    else hasDrawn = true;
    const p = getPos(e);
    ctx.beginPath(); ctx.moveTo(p.x, p.y);
  });
  canvas.addEventListener('mousemove', e => {
    if (!isDrawing) return;
    const p = getPos(e);
    ctx.lineTo(p.x, p.y); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(p.x, p.y);
  });
  const stop = () => { isDrawing = false; ctx.beginPath(); };
  canvas.addEventListener('mouseup',    stop);
  canvas.addEventListener('mouseleave', stop);

  canvas.addEventListener('touchstart', e => {
    e.preventDefault(); saveHistory();
    isDrawing = true;
    if (!hasDrawn && onFirstDraw) { hasDrawn = true; onFirstDraw(); }
    else hasDrawn = true;
    const p = getPos(e); ctx.beginPath(); ctx.moveTo(p.x, p.y);
  }, { passive: false });
  canvas.addEventListener('touchmove', e => {
    e.preventDefault(); if (!isDrawing) return;
    const p = getPos(e); ctx.lineTo(p.x, p.y); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(p.x, p.y);
  }, { passive: false });
  canvas.addEventListener('touchend', stop);

  // Serialize drawing into hidden input on submit
  canvas.closest('form').addEventListener('submit', () => {
    if (hasDrawn) dataInput.value = canvas.toDataURL('image/png');
  }, true);

  initCanvas();

  return {
    setColor(c)  { penColor = c; mode = 'pen'; applyStyle(); },
    setSize(s)   { penSize = s; applyStyle(); },
    setPen()     { mode = 'pen'; applyStyle(); },
    setEraser()  { mode = 'eraser'; applyStyle(); },
    undo()       { if (history.length) { ctx.putImageData(history.pop(), 0, 0); applyStyle(); } },
    clear()      {
      saveHistory();
      ctx.fillStyle = '#fff';
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      applyStyle();
    },
  };
}
