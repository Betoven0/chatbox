#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ────────────────────────────────────────────────────────────────
# MONKEY-PATCH Updater __slots__ para PTB 20.x–21.x
# Debe ir antes de importar telegram.ext
# ────────────────────────────────────────────────────────────────
import importlib
try:
    _upd = importlib.import_module("telegram.ext._updater")
    Updater = _upd.Updater
    if hasattr(Updater, "__slots__"):
        slots = list(Updater.__slots__)
        if "_Updater__polling_cleanup_cb" not in slots:
            slots.append("_Updater__polling_cleanup_cb")
            Updater.__slots__ = tuple(slots)
except (ImportError, ModuleNotFoundError):
    pass

import os
import logging
import unicodedata

import pandas as pd
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ────────────────────────────────────────────────────────────────
# 1. CARGAR .env
# ────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN    = os.getenv("TELEGRAM_TOKEN")
CSV_PATH = os.getenv("CSV_PATH")

# ────────────────────────────────────────────────────────────────
# 2. LOGGING
# ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ────────────────────────────────────────────────────────────────
# 3. LEER CSV (latin1)
# ────────────────────────────────────────────────────────────────
try:
    df = pd.read_csv(CSV_PATH, encoding="latin1")
    logger.info("CSV cargado. Columnas: %s", df.columns.tolist())
except Exception as e:
    logger.error("Error al leer CSV: %s", e)
    df = pd.DataFrame()

def quitar_acentos(texto: str) -> str:
    if not isinstance(texto, str):
        return texto
    n = unicodedata.normalize("NFD", texto)
    return "".join(c for c in n if unicodedata.category(c) != "Mn")

def buscar_info(texto: str) -> str:
    partes = texto.strip().split()
    if len(partes) < 2:
        return "⚠️ Escribe al menos nombre(s) y apellido paterno."
    paterno = quitar_acentos(partes[-1].lower())
    nombres = quitar_acentos(" ".join(partes[:-1]).lower())

    nombres_norm  = df["Nombre"].astype(str).apply(lambda x: quitar_acentos(x.lower()))
    paternos_norm = df["Paterno"].astype(str).apply(lambda x: quitar_acentos(x.lower()))

    mask = nombres_norm.str.contains(nombres, na=False) & paternos_norm.str.contains(paterno, na=False)
    resultados = df[mask]
    if resultados.empty:
        return "❌ No se encontró ningún alumno."

    outs = []
    for _, row in resultados.iterrows():
        líneas = [f"{col}: {row[col]}" for col in df.columns]
        outs.append("📄\n" + "\n".join(líneas))
    return "\n\n".join(outs)

# ────────────────────────────────────────────────────────────────
# 4. HANDLERS
# ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Muestra menú inicial como respuesta a /start."""
    if df.empty:
        return await update.message.reply_text("⚠️ No hay datos para mostrar.")

    buttons = [
        [InlineKeyboardButton(n, callback_data=f"al__{n}")]
        for n in df["Nombre"].unique()
    ]
    buttons.append([InlineKeyboardButton("🔄 Otro alumno", callback_data="back")])
    kb = InlineKeyboardMarkup(buttons)

    await update.message.reply_text("👋 Selecciona un alumno:", reply_markup=kb)


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Maneja todos los callback_data."""
    query = update.callback_query
    data  = query.data
    await query.answer()

    # --- 1) Datos generales ---
    if data.startswith("al__"):
        nombre = data.split("al__", 1)[1]
        sub    = df[df["Nombre"] == nombre]
        if sub.empty:
            return await query.edit_message_text("❌ Alumno no encontrado.")

        r = sub.iloc[0]
        resumen = (
            f"*{r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
            f"Carrera: {r.get('Carrera','N/A')}\n"
            f"Matrícula: {r.get('Matricula','N/A')}\n"
            f"Promedio general: {r.get('Calificacion','N/A')}\n"
            f"Cuatrimestre: {r.get('Cuatrimestre','N/A')}\n"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔢 Ver calificaciones", callback_data=f"grades__{nombre}")],
            [InlineKeyboardButton("🔄 Otro alumno",         callback_data="back")]
        ])
        return await query.edit_message_text(resumen, parse_mode="Markdown", reply_markup=kb)

    # --- 2) Calificaciones por materia ---
    if data.startswith("grades__"):
        nombre = data.split("grades__", 1)[1]
        sub    = df[df["Nombre"] == nombre]
        if sub.empty:
            return await query.edit_message_text("❌ Alumno no encontrado.")

        lines = ["*Calificaciones por materia:*"]
        for _, row in sub.iterrows():
            mat = row.get("Materia","N/A")
            cal = row.get("Calificacion","N/A")
            lines.append(f"- {mat}: {cal}")
        texto = "\n".join(lines)

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Ver datos generales", callback_data=f"al__{nombre}")],
            [InlineKeyboardButton("🔄 Otro alumno",           callback_data="back")]
        ])
        return await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb)

    # --- 3) Volver al menú inicial sin usar update.message ---
    if data == "back":
        buttons = [
            [InlineKeyboardButton(n, callback_data=f"al__{n}")]
            for n in df["Nombre"].unique()
        ]
        buttons.append([InlineKeyboardButton("🔄 Otro alumno", callback_data="back")])
        kb = InlineKeyboardMarkup(buttons)

        return await query.edit_message_text("👋 Selecciona un alumno:", reply_markup=kb)


async def text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Búsqueda libre por mensaje de texto."""
    respuesta = buscar_info(update.message.text)
    MAX       = 4000
    for i in range(0, len(respuesta), MAX):
        await update.message.reply_text(respuesta[i : i + MAX])


# ────────────────────────────────────────────────────────────────
# 5. MAIN
# ────────────────────────────────────────────────────────────────
def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text))

    logger.info("Bot arrancado correctamente.")
    app.run_polling()

if __name__ == "__main__":
    main()