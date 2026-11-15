(() => {
  const navigation = performance.getEntriesByType('navigation')[0];
  if (navigation && navigation.type !== 'navigate') return;

  // The VAE course continues on a separate site after its receipt.
  const forward = document.getElementById('vaeForward');
  if (forward) {
    const link = document.getElementById('vaeLink');
    forward.hidden = false;
    const timer = window.setTimeout(() => window.location.replace(link.href), 6500);
    document.getElementById('pauseForward').addEventListener('click', () => {
      window.clearTimeout(timer);
      forward.hidden = true;
      link.focus();
    });
    window.addEventListener('pagehide', () => {
      window.clearTimeout(timer);
      forward.hidden = true;
    }, { once: true });
  }

  const motion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const canvas = document.getElementById('receiptFireworks');
  if (motion.matches || document.hidden || !canvas) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  const colors = ['#ffd479', '#fff0c5', '#edab6d', '#e6c0e1', '#b8d5e5'];
  const flight = 420;
  const bloom = 1320;
  const bursts = [
    { x: .18, y: .28, delay: 0 },
    { x: .82, y: .24, delay: 240 },
    { x: .06, y: .59, delay: 680 },
    { x: .64, y: .16, delay: 1000 },
    { x: .91, y: .55, delay: 1390 },
    { x: .37, y: .36, delay: 1660 },
  ];
  let width, height, radius, frame;
  function resize() {
    const scale = Math.min(window.devicePixelRatio || 1, 2);
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    radius = Math.min(width * .19, height * .19, 150);
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.lineCap = 'round';
  }

  canvas.hidden = false;
  resize();
  const count = width < 650 ? 34 : 52;
  for (const burst of bursts) {
    burst.sparks = Array.from({ length: count }, (_, i) => {
      const angle = (i + Math.random() * .65) * 2 * Math.PI / count;
      const reach = .44 + Math.random() * .68;
      return {
        dx: Math.cos(angle) * reach,
        dy: Math.sin(angle) * reach,
        color: colors[i % colors.length],
        thickness: 1 + Math.random() * 1.45,
        ember: i % 3 === 0,
      };
    });
  }

  function drawBurst(burst, age) {
    const x = width * burst.x;
    const y = height * burst.y;
    if (age < flight) {
      const remaining = 1 - age / flight;
      const tipY = y + remaining * remaining * height * .34;
      const tail = 7 + remaining * 36;
      const gradient = ctx.createLinearGradient(x, tipY, x, tipY + tail);
      gradient.addColorStop(0, '#ffe4a1');
      gradient.addColorStop(1, 'rgba(255,211,138,0)');
      ctx.globalAlpha = .9;
      ctx.strokeStyle = gradient;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(x, tipY);
      ctx.lineTo(x - tail * .12, tipY + tail);
      ctx.stroke();
      return;
    }
    const t = (age - flight) / bloom;
    const reach = radius * (1 - (1 - t) ** 2.7);
    const tail = radius * (1 - (1 - Math.max(0, t - .075)) ** 2.7);
    const fall = radius * .50 * t * t;
    ctx.globalAlpha = (1 - t * t) * .96;
    for (const spark of burst.sparks) {
      ctx.lineWidth = spark.thickness;
      ctx.strokeStyle = spark.color;
      ctx.beginPath();
      ctx.moveTo(x + spark.dx * tail, y + spark.dy * tail + fall - 2 * t);
      ctx.lineTo(x + spark.dx * reach, y + spark.dy * reach + fall);
      ctx.stroke();
      if (spark.ember) {
        const emberX = x + spark.dx * reach;
        const emberY = y + spark.dy * reach + fall;
        ctx.fillStyle = '#ffdfa0';
        ctx.beginPath();
        ctx.arc(emberX, emberY, Math.max(.3, 1.8 * (1 - t)), 0, Math.PI * 2);
        ctx.fill();
        ctx.fillRect(x + spark.dx * tail * .76, y + spark.dy * tail * .76 + fall, 1.25, 1.25);
      }
    }
  }

  function stop() {
    window.cancelAnimationFrame(frame);
    canvas.hidden = true;
    canvas.width = canvas.height = 0;
    window.removeEventListener('resize', resize);
    window.removeEventListener('pagehide', stop);
    motion.removeEventListener('change', stop);
    document.removeEventListener('visibilitychange', stop);
  }

  const start = performance.now();
  function draw(now) {
    const elapsed = now - start;
    ctx.clearRect(0, 0, width, height);
    for (const burst of bursts) {
      const age = elapsed - burst.delay;
      if (age >= 0 && age < flight + bloom) drawBurst(burst, age);
    }
    if (elapsed < bursts[bursts.length - 1].delay + flight + bloom) {
      frame = window.requestAnimationFrame(draw);
    } else {
      stop();
    }
  }
  window.addEventListener('resize', resize);
  window.addEventListener('pagehide', stop);
  motion.addEventListener('change', stop);
  document.addEventListener('visibilitychange', stop);
  frame = window.requestAnimationFrame(draw);
})();
