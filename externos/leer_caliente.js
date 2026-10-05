// Lee los momios "a ganar" de una página de liga de Caliente o Codere México (misma plataforma, sin iniciar sesión).
// Se ejecuta en el navegador durante una sesión con Claude; devuelve JSON compacto de los próximos 50 h:
// [[equipo1, equipo2, inicio UTC, momio1, momio empate | null, momio2, enlace del partido], ...]
// Formato A (deportes de EE. UU.): dos filas por partido y columna "A Ganar" (td.mkt-sort-H2HT).
// Formato B (fútbol): una fila por partido con tres botones: local, empate ("Empate" o "X") y visitante.
// Se omiten los partidos en vivo (botones "inplay" o deshabilitados). La página muestra la hora en GMT-6.
(() => {
  const MESES = { ene: 0, feb: 1, mar: 2, abr: 3, may: 4, jun: 5, jul: 6, ago: 7, sep: 8, oct: 9, nov: 10, dic: 11 };
  const hoy = new Date();
  const fecha = (hora, dia) => {
    const [hh, mm] = (hora || '').trim().split(':').map(Number);
    const [d, m] = (dia || '').trim().split(/\s+/);
    const mes = MESES[(m || '').slice(0, 3).toLowerCase()];
    if (isNaN(hh) || isNaN(mm) || mes == null) return null;
    const anio = hoy.getUTCFullYear() + (mes < hoy.getUTCMonth() - 6 ? 1 : 0);
    return new Date(Date.UTC(anio, mes, +d, hh + 6, mm)).toISOString();
  };
  // La fracción es exacta; el decimal de la página viene redondeado (−197 se muestra como 1.51)
  const decimal = b => {
    const f = b?.querySelector('.price.frac')?.textContent.match(/(\d+)\/(\d+)/);
    return f ? Math.round((1 + f[1] / f[2]) * 10000) / 10000 : parseFloat(b?.querySelector('.price.dec')?.textContent);
  };
  const idEvento = b => (b?.className.match(/ev-(\d+)/) || [])[1];
  const enVivo = b => b.classList.contains('inplay') || b.disabled;
  const hora = tr => {
    const t = tr.querySelector('span.time'), d = tr.querySelector('span.date');
    return t && d ? fecha(t.textContent, d.textContent) : null;
  };
  const partidos = {};
  for (const a of document.querySelectorAll('td.event-name a[title]')) {
    const tr = a.closest('tr');
    const b = tr.querySelector('td.mkt-sort-H2HT button.price');
    if (!b || enVivo(b)) continue;
    const p = partidos[idEvento(b)] = partidos[idEvento(b)] || { equipos: [], momios: {} };
    const nombre = a.title.trim();
    p.equipos.push(nombre);
    p.momios[nombre] = decimal(b);
    p.inicio = p.inicio || hora(tr);
    p.url = p.url || a.href;
  }
  for (const tr of document.querySelectorAll('tr')) {
    const bs = [...tr.querySelectorAll(':scope > td.seln button.price')];
    if (bs.length !== 3 || bs.some(enVivo)) continue;
    const nombres = bs.map(b => b.querySelector('.seln-name')?.textContent.trim());
    partidos[idEvento(bs[0])] = {
      equipos: [nombres[0], nombres[2]], inicio: hora(tr), url: tr.querySelector('a[href*="/e/"]')?.href,
      momios: Object.fromEntries(bs.map((b, i) => [i === 1 ? 'Empate' : nombres[i], decimal(b)])),
    };
  }
  const limite = Date.now() + 50 * 3.6e6;
  return JSON.stringify(Object.values(partidos)
    .filter(p => p.equipos.length === 2 && p.inicio && new Date(p.inicio) < limite && Object.values(p.momios).every(x => x > 1))
    .map(p => [p.equipos[0], p.equipos[1], p.inicio.slice(0, 16), p.momios[p.equipos[0]], p.momios.Empate ?? null, p.momios[p.equipos[1]], p.url || null]));
})()
