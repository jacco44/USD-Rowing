(function () {
  'use strict';

  // ── Canvas confetti ────────────────────────────────────────────────────────
  var cnv = null, ctx2d = null, particles = [], raf = null;
  var COLORS = ['#22c55e','#eab308','#38bdf8','#f472b6','#a78bfa','#fb923c','#f43f5e','#34d399','#fbbf24','#60a5fa'];

  function initCanvas() {
    if (cnv) return;
    cnv = document.createElement('canvas');
    cnv.style.cssText = 'position:fixed;inset:0;width:100%;height:100%;z-index:9999;pointer-events:none';
    document.body.appendChild(cnv);
    resizeCanvas();
    window.addEventListener('resize', resizeCanvas);
  }

  function resizeCanvas() {
    if (!cnv) return;
    cnv.width = window.innerWidth;
    cnv.height = window.innerHeight;
  }

  function Particle(opts) {
    opts = opts || {};
    var cx = opts.cx != null ? opts.cx : Math.random() * (cnv ? cnv.width : window.innerWidth);
    this.x = cx;
    this.y = opts.cy != null ? opts.cy : -10;
    this.vx = (Math.random() - 0.5) * (opts.spread || 7);
    this.vy = Math.random() * 2.5 + (opts.minV || 1.5);
    this.size = Math.random() * 9 + 4;
    this.color = COLORS[Math.floor(Math.random() * COLORS.length)];
    this.rot = Math.random() * Math.PI * 2;
    this.rotV = (Math.random() - 0.5) * 0.28;
    this.life = 1;
    this.decay = Math.random() * 0.007 + 0.003;
    this.shape = Math.random() < 0.55 ? 'rect' : 'circle';
    this.gravity = opts.gravity || 0.11;
    this.wobble = Math.random() * 0.04;
    this.wobbleT = Math.random() * Math.PI * 2;
  }

  Particle.prototype.update = function () {
    this.wobbleT += 0.08;
    this.x += this.vx + Math.sin(this.wobbleT) * this.wobble;
    this.vy += this.gravity;
    this.y += this.vy;
    this.rot += this.rotV;
    this.life -= this.decay;
    this.vx *= 0.98;
  };

  Particle.prototype.draw = function () {
    ctx2d.save();
    ctx2d.globalAlpha = Math.max(0, this.life);
    ctx2d.translate(this.x, this.y);
    ctx2d.rotate(this.rot);
    ctx2d.fillStyle = this.color;
    if (this.shape === 'rect') {
      ctx2d.fillRect(-this.size / 2, -this.size / 4, this.size, this.size / 2);
    } else {
      ctx2d.beginPath();
      ctx2d.arc(0, 0, this.size / 2.5, 0, Math.PI * 2);
      ctx2d.fill();
    }
    ctx2d.restore();
  };

  function renderLoop() {
    ctx2d.clearRect(0, 0, cnv.width, cnv.height);
    particles = particles.filter(function (p) { return p.life > 0 && p.y < cnv.height + 40; });
    particles.forEach(function (p) { p.update(); p.draw(); });
    if (particles.length > 0) {
      raf = requestAnimationFrame(renderLoop);
    } else {
      cancelAnimationFrame(raf);
      if (cnv) { cnv.remove(); cnv = null; ctx2d = null; }
    }
  }

  function burst(count, opts) {
    initCanvas();
    ctx2d = cnv.getContext('2d');
    for (var i = 0; i < count; i++) {
      particles.push(new Particle(opts));
    }
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(renderLoop);
  }

  // Burst from a specific screen point (goal-complete button click)
  function burstFrom(el, count, opts) {
    var rect = el ? el.getBoundingClientRect() : null;
    var cx = rect ? rect.left + rect.width / 2 : window.innerWidth / 2;
    var cy = rect ? rect.top + rect.height / 2 : window.innerHeight / 3;
    opts = Object.assign({}, opts || {}, { cx: cx, cy: cy });
    burst(count, opts);
  }

  // ── Emoji burst overlay ────────────────────────────────────────────────────
  function showEmojiBurst(emojis, intensity) {
    var count = intensity === 'epic' ? 10 : intensity === 'moderate' ? 6 : 4;
    var overlay = document.createElement('div');
    overlay.style.cssText = 'position:fixed;inset:0;z-index:9998;pointer-events:none;overflow:hidden';
    document.body.appendChild(overlay);
    for (var i = 0; i < count; i++) {
      (function (idx) {
        setTimeout(function () {
          var em = document.createElement('span');
          em.textContent = emojis[idx % emojis.length];
          var left = 10 + Math.random() * 80;
          var top = 10 + Math.random() * 70;
          em.style.cssText = [
            'position:absolute',
            'font-size:' + (2.5 + Math.random() * 1.5) + 'rem',
            'left:' + left + '%',
            'top:' + top + '%',
            'opacity:0',
            'transform:scale(0.3) translateY(20px)',
            'transition:opacity 0.35s ease, transform 0.35s cubic-bezier(0.34,1.56,0.64,1)',
          ].join(';');
          overlay.appendChild(em);
          requestAnimationFrame(function () {
            requestAnimationFrame(function () {
              em.style.opacity = '1';
              em.style.transform = 'scale(1) translateY(0)';
            });
          });
          setTimeout(function () {
            em.style.opacity = '0';
            em.style.transform = 'scale(0.8) translateY(-30px)';
          }, 900);
        }, idx * 110);
      })(i);
    }
    setTimeout(function () { overlay.remove(); }, count * 110 + 1200);
  }

  // ── Main celebrate entry point ─────────────────────────────────────────────
  function celebrate(type) {
    if (type === 'goal_completed') {
      burst(200, { spread: 11, minV: 1.8, gravity: 0.09 });
      setTimeout(function () { burst(140, { spread: 9, minV: 1.4, gravity: 0.07, cx: window.innerWidth * 0.25 }); }, 350);
      setTimeout(function () { burst(120, { spread: 9, minV: 1.4, gravity: 0.07, cx: window.innerWidth * 0.75 }); }, 550);
      showEmojiBurst(['🏆', '🎉', '⭐', '🔥', '🎊', '💪', '🚣'], 'epic');
    } else if (type === 'perfect_workout') {
      burst(120, { spread: 9, minV: 1.8, gravity: 0.11 });
      setTimeout(function () { burst(80, { spread: 7, gravity: 0.1 }); }, 300);
      showEmojiBurst(['⭐', '💪', '🔥', '✅', '🎯'], 'moderate');
    } else if (type === 'goal_created') {
      burst(75, { spread: 7, minV: 2, gravity: 0.13 });
      showEmojiBurst(['🎯', '💪', '🚣', '🔥'], 'mild');
    } else {
      burst(60, { spread: 6, gravity: 0.13 });
    }
  }

  // ── Flash auto-dismiss ─────────────────────────────────────────────────────
  function initFlashes() {
    document.querySelectorAll('.flash').forEach(function (f, i) {
      setTimeout(function () {
        f.style.transition = 'opacity 0.55s ease, transform 0.55s ease, max-height 0.45s ease';
        f.style.opacity = '0';
        f.style.transform = 'translateX(24px)';
        setTimeout(function () { if (f.parentNode) f.parentNode.removeChild(f); }, 580);
      }, 4200 + i * 250);
    });
  }

  // ── Animated counters ──────────────────────────────────────────────────────
  function animateCounter(el) {
    var raw = el.textContent.trim();
    if (raw === '—' || raw === '') return;
    var target = parseFloat(raw);
    if (isNaN(target) || target <= 0) return;
    var isFloat = raw.indexOf('.') !== -1;
    var duration = 900;
    var start = null;
    function tick(now) {
      if (!start) start = now;
      var t = Math.min((now - start) / duration, 1);
      var ease = 1 - Math.pow(1 - t, 3);
      var val = target * ease;
      el.textContent = isFloat ? val.toFixed(2) : Math.floor(val).toString();
      if (t < 1) requestAnimationFrame(tick);
      else el.textContent = raw;
    }
    requestAnimationFrame(tick);
  }

  // ── Entrance animations ────────────────────────────────────────────────────
  function initEntrances() {
    var els = document.querySelectorAll('.stat-card, .panel, .goal-card, .form-panel, .data-table');
    els.forEach(function (el, i) {
      el.style.opacity = '0';
      el.style.transform = 'translateY(22px)';
      el.style.transition = 'opacity 0.42s ease, transform 0.42s cubic-bezier(0.22,1,0.36,1)';
      setTimeout(function () {
        el.style.opacity = '';
        el.style.transform = '';
      }, 60 + i * 55);
    });
    setTimeout(function () {
      document.querySelectorAll('.stat-value').forEach(animateCounter);
    }, 350);
  }

  // ── Nav active state ───────────────────────────────────────────────────────
  function markActiveNav() {
    var path = window.location.pathname;
    document.querySelectorAll('.app-nav a').forEach(function (a) {
      var href = a.getAttribute('href');
      if (href && href !== '/' && path.indexOf(href) === 0) {
        a.classList.add('nav-active');
      }
    });
  }

  // ── Init ───────────────────────────────────────────────────────────────────
  document.addEventListener('DOMContentLoaded', function () {
    var type = document.body.dataset.celebrate;
    if (type) celebrate(type);
    initFlashes();
    initEntrances();
    markActiveNav();
  });
})();
