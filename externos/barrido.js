// Barrido de una casa mexicana (Caliente o Codere, misma plataforma) desde su propia página, sin iniciar sesión.
// Se ejecuta en el navegador con la casa abierta: pide las páginas de las ligas con fetch (mismo sitio, sin navegar),
// lee el momio "a ganar" de cada partido de las próximas 50 h y luego abre los partidos más próximos para buscar
// "momios mejorados" del resultado final. Devuelve JSON compacto:
//   {"p": [[deporte, equipo1, equipo2, inicio UTC, momio1, empate | null, momio2, enlace], ...],
//    "m": [[enlace, {selección: momio}], ...]}
// `paginas` = {clave de deporte de The Odds API o prefijo ("soccer"): ruta de la página}.
async (paginas, maxPartidos = 30) => {
  const MESES = { ene: 0, feb: 1, mar: 2, abr: 3, may: 4, jun: 5, jul: 6, ago: 7, sep: 8, oct: 9, nov: 10, dic: 11 };
  const hoy = new Date();
  const fecha = (hora, dia) => {
    const [hh, mm] = (hora || '').trim().split(':').map(Number);
    const [d, m] = (dia || '').trim().split(/\s+/);
    const mes = MESES[(m || '').slice(0, 3).toLowerCase()];
    if (isNaN(hh) || isNaN(mm) || mes == null) return null;
    const anio = hoy.getUTCFullYear() + (mes < hoy.getUTCMonth() - 6 ? 1 : 0);
    return new Date(Date.UTC(anio, mes, +d, hh + 6, mm)).toISOString();  // la página muestra GMT-6
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
  const absoluta = h => h ? new URL(h, location.origin).href : null;
  // La casa rechaza ráfagas (503): pocas páginas a la vez y un reintento con pausa
  const espera = ms => new Promise(r => setTimeout(r, ms));
  const pagina = async ruta => {
    for (let intento = 0; intento < 3; intento++) {
      const r = await fetch(ruta);
      if (r.ok) return new DOMParser().parseFromString(await r.text(), 'text/html');
      await espera(2000 * (intento + 1));
    }
    throw new Error('no cargó ' + ruta);
  };
  const enTandas = async (lista, fn, n = 3) => {
    for (let i = 0; i < lista.length; i += n) {
      await Promise.all(lista.slice(i, i + n).map(fn));
      await espera(400);
    }
  };

  const leer = doc => {
    const partidos = {};
    for (const a of doc.querySelectorAll('td.event-name a[title]')) {  // deportes de EE. UU.: dos filas por partido
      const tr = a.closest('tr');
      const b = tr.querySelector('td.mkt-sort-H2HT button.price');
      if (!b || enVivo(b)) continue;
      const p = partidos[idEvento(b)] = partidos[idEvento(b)] || { equipos: [], momios: {} };
      const nombre = a.title.trim();
      p.equipos.push(nombre);
      p.momios[nombre] = decimal(b);
      p.inicio = p.inicio || hora(tr);
      p.url = p.url || absoluta(a.getAttribute('href'));
    }
    for (const tr of doc.querySelectorAll('tr')) {  // fútbol: local, empate y visitante en una fila
      const bs = [...tr.querySelectorAll(':scope > td.seln button.price')];
      if (bs.length !== 3 || bs.some(enVivo)) continue;
      // En hockey la fila de 3 es "tiempo regular" (con empate): si el partido ya tiene "a ganar" se queda ese,
      // que es el mismo mercado que Pinnacle
      if (partidos[idEvento(bs[0])]) continue;
      const nombres = bs.map(b => b.querySelector('.seln-name')?.textContent.trim());
      partidos[idEvento(bs[0])] = {
        equipos: [nombres[0], nombres[2]], inicio: hora(tr),
        url: absoluta(tr.querySelector('a[href*="/e/"]')?.getAttribute('href')),
        momios: Object.fromEntries(bs.map((b, i) => [i === 1 ? 'Empate' : nombres[i], decimal(b)])),
      };
    }
    return Object.values(partidos);
  };

  const limite = Date.now() + 50 * 3.6e6, vistos = new Map();
  await enTandas(Object.entries(paginas), async ([deporte, ruta]) => {
    try {
      for (const p of leer(await pagina(ruta))) {
        if (p.equipos.length !== 2 || !p.inicio || new Date(p.inicio) > limite || !Object.values(p.momios).every(x => x > 1)) continue;
        const clave = p.url || p.equipos.join('|');
        // una liga exacta gana sobre una sección general del deporte
        if (!vistos.has(clave) || deporte.includes('_')) vistos.set(clave, { ...p, deporte });
      }
    } catch (e) { /* página que no cargó: se sigue con las demás */ }
  });
  // Solo los partidos que sigue el bot (lista pública del tablero): misma hora ±6 h y alguna palabra en común
  const palabras = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .split(/[^a-z0-9]+/).filter(w => w.length >= 4);
  let delBot = null;
  try { delBot = await (await fetch('https://diegolany.github.io/simulador/partidos.json', { cache: 'no-store' })).json(); } catch (e) { }
  const loSigue = p => !delBot || delBot.some(([dep, l, v, ini]) =>
    dep.startsWith(p.deporte.split('_')[0]) && Math.abs(new Date(ini + 'Z') - new Date(p.inicio)) < 6 * 3.6e6 &&
    palabras(p.equipos.join(' ')).some(w => palabras(l + ' ' + v).includes(w)));
  const partidos = [...vistos.values()].filter(loSigue);

  // Momios mejorados del resultado final en los partidos más próximos
  const proximos = partidos.filter(p => p.url && new Date(p.inicio) < Date.now() + 36 * 3.6e6)
    .sort((a, b) => a.inicio.localeCompare(b.inicio)).slice(0, maxPartidos);
  const mejorados = [];
  await enTandas(proximos, async p => {
      try {
        const doc = await pagina(p.url);
        for (const m of doc.querySelectorAll('div.mkt')) {
          const titulo = m.querySelector('.mkt-name')?.textContent || '';
          if (!/mejorad|supercuota|aumentad/i.test(titulo) || !/resultado final|a ganar|ganador|l[ií]nea de dinero|moneyline|1x2/i.test(titulo)) continue;
          const bs = [...m.querySelectorAll('button.price')].filter(b => !enVivo(b));
          if (bs.length < 2 || bs.length > 3) continue;
          mejorados.push([p.url, Object.fromEntries(bs.map(b => [
            /^(empate|x)$/i.test((b.title || '').trim()) ? 'Empate' : (b.title || '').trim(), decimal(b)]))]);
        }
      } catch (e) { /* partido que no cargó */ }
  });
  return JSON.stringify({
    p: partidos.map(p => [p.deporte, p.equipos[0], p.equipos[1], p.inicio.slice(0, 16), p.momios[p.equipos[0]],
                          p.momios.Empate ?? null, p.momios[p.equipos[1]], p.url || null]),
    m: mejorados,
  });
}
