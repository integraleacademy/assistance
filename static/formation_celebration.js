(() => {
  const navigation = performance.getEntriesByType('navigation')[0];
  if (navigation?.type === 'back_forward') return;

  // The VAE course continues on a separate site after its receipt.
  const forward = document.getElementById('vaeForward');
  if (forward && navigation?.type !== 'reload') {
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

  const colors = ['#ffdd99', '#fff4d4', '#f5ad61', '#ecc77a', '#b8dce3'];
  const flight = 460;
  const bloom = 1530;
  const bursts = [
    { x: .12, y: .27, delay: 0 },
    { x: .84, y: .22, delay: 210, willow: true },
    { x: .60, y: .14, delay: 710 },
    { x: .08, y: .52, delay: 1110, willow: true },
    { x: .94, y: .49, delay: 1460 },
    { x: .31, y: .17, delay: 1980, willow: true },
    { x: .73, y: .31, delay: 2130, willow: true },
  ];
  let width, height, radius, frame;
  function resize() {
    const scale = Math.min(window.devicePixelRatio || 1, 2);
    width = canvas.clientWidth;
    height = canvas.clientHeight;
    radius = Math.min(width * .24, height * .24, 184);
    canvas.width = Math.round(width * scale);
    canvas.height = Math.round(height * scale);
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.lineCap = 'round';
  }

  canvas.hidden = false;
  resize();
  const count = width < 650 ? 40 : 64;
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
    const tailTime = Math.max(0, t - (burst.willow ? .18 : .105));
    const tail = radius * (1 - (1 - tailTime) ** 2.7);
    const gravity = radius * (burst.willow ? 1.1 : .48);
    const fall = gravity * t * t;
    const tailFall = gravity * tailTime * tailTime;
    ctx.globalAlpha = (1 - t * t) * .96;
    for (const spark of burst.sparks) {
      ctx.lineWidth = spark.thickness;
      ctx.strokeStyle = spark.color;
      ctx.beginPath();
      ctx.moveTo(x + spark.dx * tail, y + spark.dy * tail + tailFall);
      ctx.quadraticCurveTo(x + spark.dx * reach, y + spark.dy * reach + tailFall,
        x + spark.dx * reach, y + spark.dy * reach + fall);
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
