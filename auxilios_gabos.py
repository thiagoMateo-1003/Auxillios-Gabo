"""Auxilios GABO: monitor Claims + calculo orientativo de recorridos y precio.

Basado en monitor24siete.py. Requiere playwright y requests.
No envia ofertas ni acepta asistencias en Claims.
Incluye integracion con Bot de Telegram para comandos de control (/prender, /apagar, /estado).
"""
import http.server
import threading
import socket
import argparse
import asyncio
import json
import math
import os
import time
import json
from pathlib import Path
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from playwright.async_api import async_playwright

URL_CLAIMS = "https://24siete.claimservices.com.ar/asistencias/prestador"
URL_GPS = "https://cotrack.conecty.io/api/positions"
URL_OSRM = "https://router.project-osrm.org/route/v1/driving/"
BASE = (-31.434272, -64.503046)
ID_CAMION = 5626
PRECIO_KM = 2000
REVISION_SEGUNDOS = 3600
ESTADO_SEGUNDOS = 1800
GPS_ACTUAL_MINUTOS = 15
BASE_RADIO_METROS = 150
CHAT_IDS = ["8328439710", "8882694706"]

CARPETA = Path(__file__).resolve().parent
# Archivos independientes del monitor original.
ARCHIVO_IDS = CARPETA / "ids_cotizador_nuevo.json"
ARCHIVO_PENDIENTES = CARPETA / "pendientes_cotizador_nuevo.json"
ARCHIVO_COMPLETADAS = CARPETA / "completadas_cotizador_nuevo.json"
ARCHIVO_FALLIDAS = CARPETA / "fallidas_cotizador_nuevo.json"
CARPETA_TOKENS = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AuxiliosGabo"
TOKEN_TELEGRAM_FILE = CARPETA_TOKENS / "token_telegram.txt"

# Estado global de ejecucion para el control del Bot
MONITOREO_ACTIVO = True


def leer_token_telegram():
    if "TOKEN_TELEGRAM" in os.environ and os.environ["TOKEN_TELEGRAM"].strip():
        return os.environ["TOKEN_TELEGRAM"].strip()
    if TOKEN_TELEGRAM_FILE.exists():
        valor = TOKEN_TELEGRAM_FILE.read_text(encoding="utf-8-sig").strip()
        if valor:
            return valor
    raise ValueError("Token Telegram no configurado en entorno ni en archivo local")

def iniciar_servidor_web():
    """Inicia un servidor HTTP básico para cumplir con el requisito de puertos de Render."""
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Auxilios GABO Bot is running!")

    port = int(os.environ.get("PORT", 10000))
    server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    print(f"Servidor web fantasma escuchando en el puerto {port}")
    server.serve_forever()

def cargar_ids(ruta):
    if not ruta.exists():
        return set()
    datos = json.loads(ruta.read_text(encoding="utf-8"))
    if not isinstance(datos, list):
        raise ValueError(f"Formato invalido: {ruta.name}")
    return {str(x) for x in datos}


def guardar_ids(ruta, ids):
    temporal = ruta.with_suffix(".tmp")
    temporal.write_text(json.dumps(sorted(ids), indent=2), encoding="utf-8")
    os.replace(temporal, ruta)


def enviar_telegram_sync(mensaje):
    token = leer_token_telegram()
    resultado = True
    for chat_id in CHAT_IDS:
        try:
            respuesta = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": mensaje}, timeout=10,
            )
            respuesta.raise_for_status()
            if not respuesta.json().get("ok"):
                raise ValueError("Telegram no confirmo la entrega")
        except Exception as error:
            print(f"Telegram error ({chat_id}): {type(error).__name__}: {error}")
            resultado = False
    return resultado


async def telegram(mensaje):
    try:
        return await asyncio.to_thread(enviar_telegram_sync, mensaje)
    except Exception as error:
        print(f"No se pudo usar Telegram: {type(error).__name__}: {error}")
        return False


async def procesar_comandos_telegram():
    """Escucha comandos entrantes de Telegram para controlar la ejecucion (/prender, /apagar, /estado)."""
    global MONITOREO_ACTIVO
    token = leer_token_telegram()
    offset = None
    print("Bot de control de Telegram iniciado en segundo plano.")
    
    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates"
            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = offset
            
            respuesta = await asyncio.to_thread(requests.get, url, params=params, timeout=25)
            if respuesta.status_code == 200:
                datos = respuesta.json()
                if datos.get("ok"):
                    for update in datos.get("result", []):
                        offset = update["update_id"] + 1
                        mensaje = update.get("message", {})
                        texto = mensaje.get("text", "").strip().casefold()
                        chat_id = str(mensaje.get("chat", {}).get("id", ""))

                        if chat_id in CHAT_IDS:
                            if texto in ("/prender", "/activar", "prender", "activar"):
                                MONITOREO_ACTIVO = True
                                await telegram("🟢 Monitor Claims ACTIVADO. Se reanuda la revision de asistencias.")
                                print("Comando recibido: MONITOR ACTIVADO")
                            elif texto in ("/apagar", "/pausar", "apagar", "pausar"):
                                MONITOREO_ACTIVO = False
                                await telegram("🔴 Monitor Claims PAUSADO. No se revisaran nuevas asistencias hasta nuevo aviso.")
                                print("Comando recibido: MONITOR PAUSADO")
                            elif texto in ("/estado", "estado"):
                                estado_str = "ACTIVO 🟢" if MONITOREO_ACTIVO else "PAUSADO 🔴"
                                await telegram(f"Estado actual del servicio Auxilios GABO: {estado_str}")
        except Exception as error:
            print(f"Error procesando comandos de Telegram: {type(error).__name__}: {error}")
        
        await asyncio.sleep(2)


def metros_recta(a, b):
    lat1, lon1 = map(math.radians, a)
    lat2, lon2 = map(math.radians, b)
    h = (math.sin((lat2-lat1)/2)**2
         + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2)
    return 2 * 6371000 * math.asin(math.sqrt(min(1, h)))


def posicion_camion_sync():
    respuesta = requests.get(
        URL_GPS,
        headers={"Accept": "application/json"},
        timeout=15,
    )
    respuesta.raise_for_status()
    posiciones = respuesta.json()
    if not isinstance(posiciones, list):
        raise ValueError("Respuesta GPS inesperada")
    p = next((x for x in posiciones if str(x.get("deviceId")) == str(ID_CAMION)), None)
    if p is None or p.get("valid") is not True:
        raise ValueError("No hay posicion GPS valida del camion")
    fecha = datetime.fromisoformat(p["fixTime"].replace("Z", "+00:00"))
    minutos = (datetime.now(timezone.utc) - fecha).total_seconds() / 60
    if minutos < -2:
        raise ValueError("Hora GPS inconsistente")
    ubicacion = (float(p["latitude"]), float(p["longitude"]))
    if minutos <= GPS_ACTUAL_MINUTOS:
        return ubicacion, minutos, False
    if metros_recta(ubicacion, BASE) <= BASE_RADIO_METROS:
        return BASE, minutos, True  # Suposicion, no posicion en vivo.
    raise ValueError("GPS antiguo lejos de la base; confirmar ubicacion")


def ruta_sync(a, b):
    url = f"{URL_OSRM}{a[1]},{a[0]};{b[1]},{b[0]}"
    r = requests.get(url, params={"overview": "false"}, timeout=20)
    r.raise_for_status()
    datos = r.json()
    if datos.get("code") != "Ok" or not datos.get("routes"):
        raise ValueError("No se pudo obtener ruta")
    ruta = datos["routes"][0]
    return ruta["distance"] / 1000, ruta["duration"] / 60


def cotizar_sync(origen, destino):
    camion, edad, provisional = posicion_camion_sync()
    tramos = [
        ("Camion a origen", camion, origen),
        ("Origen a destino", origen, destino),
        ("Destino a base", destino, BASE),
    ]
    valores = []
    for nombre, inicio, fin in tramos:
        km, minutos = ruta_sync(inicio, fin)
        valores.append((nombre, km, minutos))
    total_km = sum(x[1] for x in valores)
    total_min = sum(x[2] for x in valores)
    precio = round(total_km * PRECIO_KM)
    return valores, total_km, total_min, precio, edad, provisional


def informe_cotizacion(id_, datos):
    tramos, km, minutos, precio, edad, provisional = datos
    lineas = ["AUXILIOS GABO - PRECIO SUGERIDO", "", f"Asistencia: {id_}", ""]
    if provisional:
        lineas.extend([
            "ATENCION: UBICACION PROVISIONAL",
            "GPS antiguo cerca de la base; se SUPONE camion en base.",
            "CONFIRMAR UBICACION antes de cotizar.", "",
        ])
    for nombre, distancia, duracion in tramos:
        lineas.append(f"{nombre}: {distancia:.2f} km | {duracion:.1f} min")
    lineas.extend([
        "", f"TOTAL: {km:.2f} km", f"CONDUCCION ESTIMADA: {minutos:.1f} min",
        "TARIFA: $2.000/km", f"PRECIO SUGERIDO: ${precio:,.0f}".replace(",", "."),
        "", f"Antiguedad GPS: {edad:.1f} min",
        "Sin trafico real, peajes, carga ni descarga.",
        "NO se envio oferta a Claims.",
    ])
    return "\n".join(lineas)


async def seleccionar_todas(pagina):
    """Mismos cuatro metodos del monitor24siete.py original."""
    print("Buscando pestana TODAS...")
    try:
        await pagina.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    await pagina.wait_for_timeout(4000)

    candidatos = [
        pagina.get_by_text("TODAS", exact=True),
        pagina.locator("a", has_text="TODAS").first,
        pagina.locator("button", has_text="TODAS").first,
        pagina.locator("text=TODAS"),
    ]
    for candidato in candidatos:
        try:
            if await candidato.count() == 1:
                elemento = candidato.first
                await elemento.wait_for(state="visible", timeout=5000)
                await elemento.click(timeout=10000)
                await pagina.wait_for_timeout(3000)
                print("Pestana TODAS seleccionada.")
                return True
            for i in range(await candidato.count()):
                elemento = candidato.nth(i)
                if await elemento.is_visible():
                    await elemento.click(timeout=10000)
                    await pagina.wait_for_timeout(3000)
                    print("Pestana TODAS seleccionada.")
                    return True
        except Exception:
            continue
    return False


async def preparar_asistencias(pagina):
    await pagina.goto(URL_CLAIMS, wait_until="domcontentloaded", timeout=30000)
    if not await seleccionar_todas(pagina):
        raise ValueError("No aparece TODAS. Revisar inicio de sesion/ASISTENCIAS; URL: " + pagina.url)


async def leer_filas(pagina):
    resultados = []
    filas = pagina.locator("tr")
    for i in range(await filas.count()):
        try:
            texto = (await filas.nth(i).inner_text(timeout=2000)).strip()
            lineas = [x.strip() for x in texto.splitlines() if x.strip()]
            if lineas and lineas[0].isdigit():
                resultados.append((lineas[0], texto))
        except Exception:
            continue
    return resultados


def extraer_coordenadas(enlace):
    url = urlparse(enlace)
    if url.hostname not in ("www.google.com", "google.com"):
        raise ValueError("Enlace de Google Maps incorrecto")
    q = parse_qs(url.query)
    puntos = []
    for clave in ("origin", "destination"):
        valor = q.get(clave, [None])[0]
        if not valor:
            raise ValueError(f"Faltan coordenadas: {clave}")
        partes = valor.split(",")
        if len(partes) != 2:
            raise ValueError("Coordenadas invalidas")
        lat, lon = map(float, partes)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Coordenadas fuera de rango")
        puntos.append((lat, lon))
    return puntos[0], puntos[1]


async def copiar_ruta(pagina, id_):
    await preparar_asistencias(pagina)
    filas = pagina.locator("tr")
    encontrada = None
    for i in range(await filas.count()):
        fila = filas.nth(i)
        try:
            celdas = fila.locator("td")
            if await celdas.count() and (await celdas.first.inner_text()).strip() == id_:
                encontrada = fila
                break
        except Exception:
            continue
    if encontrada is None:
        raise ValueError(f"Asistencia {id_} ya no visible en TODAS")

    ultima_celda = encontrada.locator("td").last
    botones_acciones = ultima_celda.locator("button")
    if await botones_acciones.count() == 0:
        botones_acciones = encontrada.locator("button")
    if await botones_acciones.count() == 0:
        raise ValueError("No se encontro boton de tres puntos en la fila")

    boton_acciones = botones_acciones.last
    await boton_acciones.scroll_into_view_if_needed()
    await boton_acciones.click(timeout=10000)
    print("Menu de acciones abierto:", id_)

    opcion_detalle = None
    nombre_opcion = None
    limite_menu = time.monotonic() + 12

    while time.monotonic() < limite_menu and opcion_detalle is None:
        menus = pagina.locator(
            ".dropdown-menu.show, .dropdown-menu:visible, [role='menu']:visible"
        )
        for objetivo in ("ver", "gestionar"):
            for i in range(await menus.count()):
                menu = menus.nth(i)
                try:
                    if not await menu.is_visible():
                        continue
                    elementos = menu.locator(
                        "a, button, [role='menuitem'], .dropdown-item, li, span"
                    )
                    for j in range(await elementos.count()):
                        elemento = elementos.nth(j)
                        if not await elemento.is_visible():
                            continue
                        texto = (await elemento.inner_text(timeout=1000)).strip().casefold()
                        if texto == objetivo:
                            opcion_detalle = elemento
                            nombre_opcion = objetivo
                            break
                    if opcion_detalle is not None:
                        break
                except Exception:
                    continue
            if opcion_detalle is not None:
                break

        if opcion_detalle is None:
            zona = await boton_acciones.bounding_box()
            if zona:
                for objetivo in ("Ver", "Gestionar"):
                    candidatos = pagina.get_by_text(objetivo, exact=True)
                    for j in range(await candidatos.count()):
                        candidato = candidatos.nth(j)
                        try:
                            if not await candidato.is_visible():
                                continue
                            caja = await candidato.bounding_box()
                            if not caja:
                                continue
                            dx = abs((zona["x"] + zona["width"] / 2) -
                                     (caja["x"] + caja["width"] / 2))
                            dy = abs((zona["y"] + zona["height"] / 2) -
                                     (caja["y"] + caja["height"] / 2))
                            if dx < 280 and dy < 320:
                                opcion_detalle = candidato
                                nombre_opcion = objetivo.casefold()
                                break
                        except Exception:
                            continue
                    if opcion_detalle is not None:
                        break

        if opcion_detalle is None:
            await asyncio.sleep(0.4)

    if opcion_detalle is None:
        visibles = []
        menus = pagina.locator(".dropdown-menu:visible, [role='menu']:visible")
        for i in range(min(await menus.count(), 3)):
            try:
                visibles.append((await menus.nth(i).inner_text())[:140])
            except Exception:
                pass
        raise ValueError(
            "No se encontro Ver ni Gestionar. Menus visibles: " + repr(visibles)
        )

    print("Abriendo detalle usando:", nombre_opcion.upper(), "-", id_)
    await opcion_detalle.click(timeout=10000)
    print("Detalle abierto:", id_)

    boton_ruta = None
    limite = time.monotonic() + 25
    while time.monotonic() < limite and boton_ruta is None:
        botones = pagina.locator("button")
        for i in range(await botones.count()):
            boton = botones.nth(i)
            try:
                if await boton.is_visible() and "copiar ruta" in (await boton.inner_text(timeout=1500)).casefold():
                    boton_ruta = boton
                    break
            except Exception:
                continue
        if boton_ruta is None:
            await asyncio.sleep(1)
    if boton_ruta is None:
        raise ValueError("No se encontro boton Copiar ruta")
    await boton_ruta.scroll_into_view_if_needed()
    await boton_ruta.click(timeout=10000)
    enlace = await pagina.evaluate("() => navigator.clipboard.readText()")
    return extraer_coordenadas(enlace)


async def trabajador_cotizaciones(pagina, cola, pendientes, completadas, fallidas):
    while True:
        id_ = await cola.get()
        try:
            if id_ in completadas:
                continue
            print("Calculando asistencia", id_)
            origen, destino = await copiar_ruta(pagina, id_)
            resultado = await asyncio.to_thread(cotizar_sync, origen, destino)
            informe = informe_cotizacion(id_, resultado)
            print("\n" + informe + "\n")
            if not await telegram(informe):
                raise ValueError("Telegram no confirmo envio a ambos celulares")
            completadas.add(id_)
            guardar_ids(ARCHIVO_COMPLETADAS, completadas)
            pendientes.discard(id_)
            guardar_ids(ARCHIVO_PENDIENTES, pendientes)
            print("Cotizacion enviada por Telegram:", id_)
        except Exception as error:
            print("Error cotizando", id_, type(error).__name__, str(error)[:200])
            fallidas.add(id_)
            guardar_ids(ARCHIVO_FALLIDAS, fallidas)
            await telegram(f"AUXILIOS GABO: no se pudo calcular asistencia {id_}. Revisar manualmente si sigue disponible.")
        finally:
            pendientes.discard(id_)
            guardar_ids(ARCHIVO_PENDIENTES, pendientes)
            cola.task_done()


async def monitor_claims(pagina, cola, conocidos, pendientes, completadas):
    ultimo_estado = time.monotonic()
    nuevas_periodo = 0
    problema_avisado = False
    while True:
        inicio = time.monotonic()
        if not MONITOREO_ACTIVO:
            await asyncio.sleep(5)
            continue
            
        try:
            await preparar_asistencias(pagina)
            datos = await leer_filas(pagina)
            if not datos:
                raise ValueError("Claims: tabla vacia o no legible")
            print(f"Claims OK: {len(datos)} asistencias visibles")
            if problema_avisado:
                await telegram("Auxilios GABO: monitor Claims recuperado.")
                problema_avisado = False
            for id_, detalle in datos:
                if id_ in conocidos:
                    continue
                if id_ not in pendientes and id_ not in completadas:
                    pendientes.add(id_)
                    guardar_ids(ARCHIVO_PENDIENTES, pendientes)
                    await cola.put(id_)
                conocidos.add(id_)
                guardar_ids(ARCHIVO_IDS, conocidos)
                nuevas_periodo += 1
                print("NUEVA ASISTENCIA:", id_)
                await telegram(
                    f"NUEVA ASISTENCIA 24SIETE\n\nID: {id_}\n\n{detalle[:1500]}\n\nCalculando recorrido y precio sugerido..."
                )
            if time.monotonic() - ultimo_estado >= ESTADO_SEGUNDOS:
                mensaje = (
                    "MONITOR 24SIETE OK\n\nLa tabla se lee correctamente."
                    f"\nNuevas asistencias en 30 min: {nuevas_periodo}"
                    f"\nCotizaciones pendientes: {len(pendientes)}"
                )
                try:
                    camion, edad, provisional = await asyncio.to_thread(posicion_camion_sync)
                    km, minutos = await asyncio.to_thread(ruta_sync, camion, BASE)
                    mensaje += f"\nGPS: {edad:.1f} min\nA base: {km:.2f} km, {minutos:.1f} min"
                    if provisional:
                        mensaje += "\nGPS antiguo: camion en base SUPUESTO, no confirmado."
                except Exception as error:
                    mensaje += "\nGPS/recorrido a base: no disponible"
                    print("GPS en estado:", type(error).__name__, str(error)[:100])
                await telegram(mensaje)
                ultimo_estado = time.monotonic()
                nuevas_periodo = 0
        except Exception as error:
            print("Problema leyendo Claims:", type(error).__name__, str(error)[:200])
            if not problema_avisado:
                await telegram("ATENCION MONITOR CLAIMS: no se pudo leer TODAS. Revisar sesion o conexion.")
                problema_avisado = True
        await asyncio.sleep(max(1, REVISION_SEGUNDOS - (time.monotonic() - inicio)))


async def principal():
    hilo_web = threading.Thread(target=iniciar_servidor_web, daemon=True)
    hilo_web.start()
    leer_token_telegram()
    conocidos = cargar_ids(ARCHIVO_IDS)
    pendientes = cargar_ids(ARCHIVO_PENDIENTES)
    completadas = cargar_ids(ARCHIVO_COMPLETADAS)
    fallidas = cargar_ids(ARCHIVO_FALLIDAS)

    if pendientes:
        cantidad = len(pendientes)
        fallidas.update(pendientes)
        guardar_ids(ARCHIVO_FALLIDAS, fallidas)
        pendientes.clear()
        guardar_ids(ARCHIVO_PENDIENTES, pendientes)
        print(f"Archivadas {cantidad} cotizaciones pendientes antiguas; sin reintentos automaticos.")

    # Iniciar escuchador de comandos de Telegram en segundo plano
    asyncio.create_task(procesar_comandos_telegram())

    async with async_playwright() as pw:
        # Modo headless activado para ejecucion en servidores/cloud
        navegador = await pw.chromium.launch(headless=True)
        contexto = await navegador.new_context(permissions=["clipboard-read", "clipboard-write"])

        # INYECCIÓN DE COOKIES AQUÍ
        archivo_cookies = Path("cookies.json")
        if archivo_cookies.exists():
            cookies = json.loads(archivo_cookies.read_text(encoding="utf-8"))
            await contexto.add_cookies(cookies)
            print("¡Cookies de sesión inyectadas correctamente!")

        pagina_monitor = await contexto.new_page()
        pagina_cotizador = await contexto.new_page()
        try:
            await pagina_monitor.goto(URL_CLAIMS, wait_until="domcontentloaded", timeout=30000)
            if not await seleccionar_todas(pagina_monitor):
                raise ValueError("No se encontro TODAS. Es necesario estar en ASISTENCIAS.")
            datos = await leer_filas(pagina_monitor)
            if not datos:
                raise ValueError("Tabla inicial vacia o ilegible")
            if not ARCHIVO_IDS.exists():
                conocidos.update(id_ for id_, _ in datos)
                guardar_ids(ARCHIVO_IDS, conocidos)
            cola = asyncio.Queue()
            print("MONITOR + COTIZADOR ACTIVADO. Tarifa: $2.000/km")
            await telegram("Auxilios GABO: monitor y cotizador activados en la nube. Usa /apagar o /prender para gestionar el servicio.")
            await asyncio.gather(
                monitor_claims(pagina_monitor, cola, conocidos, pendientes, completadas),
                trabajador_cotizaciones(pagina_cotizador, cola, pendientes, completadas, fallidas),
            )
        finally:
            await navegador.close()


async def prueba_manual(identificador):
    """Prueba aislada: no toca archivos de IDs ni inicia el monitor continuo."""
    if not identificador.isdigit():
        raise ValueError("El numero de asistencia debe contener solo digitos")

    print("=" * 54)
    print("AUXILIOS GABO - PRUEBA MANUAL DE COTIZACION")
    print(f"Asistencia: {identificador} | Tarifa: $2.000/km")
    print("No se modificaran los archivos de asistencias conocidas.")
    print("No se enviara ninguna oferta a Claims.")
    print("=" * 54)

    leer_token_telegram()

    async with async_playwright() as pw:
        navegador = await pw.chromium.launch(headless=True)
        contexto = await navegador.new_context(
            permissions=["clipboard-read", "clipboard-write"]
        )
        pagina = await contexto.new_page()
        try:
            await pagina.goto(
                URL_CLAIMS,
                wait_until="domcontentloaded",
                timeout=30000,
            )
            print(f"\nExtrayendo recorrido de la asistencia {identificador}...")
            origen, destino = await copiar_ruta(pagina, identificador)
            print("Coordenadas obtenidas correctamente.")
            print("Consultando GPS y calculando los tres recorridos...")
            resultado = await asyncio.to_thread(cotizar_sync, origen, destino)
            mensaje = informe_cotizacion(identificador, resultado)
            print("\n" + "=" * 54)
            print(mensaje)
            print("=" * 54)
            await telegram(mensaje)
            print("Prueba manual terminada. Informe enviado a Telegram.")
        finally:
            await navegador.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Monitor Claims y cotizador de Auxilios GABO"
    )
    parser.add_argument(
        "--prueba", metavar="ID", help="Cotizar manualmente una asistencia visible sin monitorear"
    )
    args = parser.parse_args()
    try:
        if args.prueba is not None:
            asyncio.run(prueba_manual(args.prueba))
        else:
            asyncio.run(principal())
    except KeyboardInterrupt:
        print("Programa detenido por el usuario")
    except Exception as error:
        print("ERROR:", type(error).__name__, str(error))