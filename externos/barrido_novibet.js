// Lector de Novibet México (novibet.mx), sin iniciar sesión. Novibet arma sus páginas en el navegador, así que hay
// que abrir cada liga y leer lo que muestra. Cada llamada lee la página abierta y lo junta en sessionStorage;
// con final = true devuelve todo en el formato de barrido.js:
//   {"p": [[deporte, local, visitante, inicio UTC, momio1, empate | null, momio2, enlace | null], ...], "m": []}
// Formatos que muestra (momios americanos, hora de México GMT-6):
//   EE. UU.:  Equipo1 / vs (o @) / Equipo2 / "mar 17:00" / Ganador / momio1 / momio2
//   Fútbol:   Local / "vie 19:00" / Visitante / 1 / momio / X / momio / 2 / momio
async (deporte, final = false) => {
  const DIAS = { dom: 0, lun: 1, mar: 2, 'mié': 3, mie: 3, jue: 4, vie: 5, 'sáb': 6, sab: 6 };
  const HORA = /^(lun|mar|mi[eé]|jue|vie|s[aá]b|dom|hoy|mañana)\.?\s+(\d{1,2}):(\d{2})$/i;
  const AMER = /^[+-]\d{3,4}$/;
  const decimal = s => { const n = parseInt(s, 10); return Math.round((n > 0 ? 1 + n / 100 : 1 + 100 / -n) * 10000) / 10000; };
  const inicio = txt => {  // fecha de México (GMT-6) → UTC
    const [, dia, hh, mm] = txt.match(HORA);
    const ahoraMx = new Date(Date.now() - 6 * 3.6e6);
    const hoyMx = ahoraMx.getUTCDay();
    const d = dia.toLowerCase();
    const adelante = d === 'hoy' ? 0 : d === 'mañana' ? 1 : (DIAS[d] - hoyMx + 7) % 7;
    const f = new Date(Date.UTC(ahoraMx.getUTCFullYear(), ahoraMx.getUTCMonth(), ahoraMx.getUTCDate() + adelante, +hh + 6, +mm));
    return f.toISOString().slice(0, 16);
  };
  const lineas = document.body.innerText.split('\n').map(s => s.trim()).filter(Boolean);
  const nuevos = [];
  for (let i = 1; i < lineas.length - 8; i++) {
    if ((lineas[i] === 'vs' || lineas[i] === '@') && HORA.test(lineas[i + 2] || '')) {  // deportes de EE. UU. ("@": visitante primero)
      const k = lineas.indexOf('Ganador', i + 3);
      if (k < 0 || k > i + 4 || !AMER.test(lineas[k + 1]) || !AMER.test(lineas[k + 2])) continue;
      nuevos.push([deporte, lineas[i - 1], lineas[i + 1], inicio(lineas[i + 2]), decimal(lineas[k + 1]), null, decimal(lineas[k + 2]), null]);
    } else if (HORA.test(lineas[i]) && lineas[i + 2] === '1' && lineas[i + 4] === 'X' && lineas[i + 6] === '2'
               && [3, 5, 7].every(j => AMER.test(lineas[i + j]))) {  // fútbol 1 X 2
      nuevos.push([deporte, lineas[i - 1], lineas[i + 1], inicio(lineas[i]), decimal(lineas[i + 3]), decimal(lineas[i + 5]),
                   decimal(lineas[i + 7]), null]);
    }
  }
  const previos = JSON.parse(sessionStorage.getItem('__novibet') || '[]');
  const todos = previos.concat(nuevos.filter(n => !previos.some(p => p[1] === n[1] && p[2] === n[2] && p[3] === n[3])));
  sessionStorage.setItem('__novibet', JSON.stringify(todos));
  if (!final) return `${nuevos.length} partidos en esta página (${todos.length} en total)`;
  sessionStorage.removeItem('__novibet');
  // Solo los partidos que sigue el bot (lista pública del tablero): misma hora ±6 h y alguna palabra en común
  const palabras = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()
    .split(/[^a-z0-9]+/).filter(w => w.length >= 4);
  let delBot = null;
  try { delBot = await (await fetch('https://diegolany.github.io/simulador/partidos.json', { cache: 'no-store' })).json(); } catch (e) { }
  const sigue = p => !delBot || delBot.some(([dep, l, v, ini]) =>
    dep.startsWith(p[0].split('_')[0]) && Math.abs(new Date(ini + 'Z') - new Date(p[3] + 'Z')) < 6 * 3.6e6 &&
    palabras(p[1] + ' ' + p[2]).some(w => palabras(l + ' ' + v).includes(w)));
  return JSON.stringify({ p: todos.filter(sigue), m: [] });
}
