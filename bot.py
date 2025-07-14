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
    """Inicio: pedir matrícula directamente"""
    await update.message.reply_text("✏️ Escribe la *matrícula* del alumno que deseas consultar.", parse_mode="Markdown")

async def buscar_matricula(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Busca alumno por matrícula ingresada en texto"""
    matricula = update.message.text.strip()
    sub = df[df["Matricula"].astype(str).str.strip() == matricula]
    if sub.empty:
        return await update.message.reply_text("❌ Matrícula no encontrada.")

    r = sub.iloc[0]
    context.user_data["matricula"] = matricula

    resumen = (
        f"*{r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
        f"Carrera: {r.get('Carrera','N/A')}\n"
        f"Matrícula: {r.get('Matricula','N/A')}\n"
        f"Promedio general: {r.get('Calificacion','N/A')}\n"
        f"Cuatrimestre: {r.get('Cuatrimestre','N/A')}\n"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Ver calificaciones", callback_data="grades")],
        [InlineKeyboardButton("🔄 Consultar otra matrícula", callback_data="back")]
    ])
    await update.message.reply_text(resumen, parse_mode="Markdown", reply_markup=kb)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    matricula = context.user_data.get("matricula", "")
    sub = df[df["Matricula"].astype(str).str.strip() == matricula]

    if sub.empty:
        return await query.edit_message_text("❌ Matrícula no encontrada.")

    r = sub.iloc[0]

    if query.data == "grades":
        lines = ["*Calificaciones por materia:*"]
        for _, row in sub.iterrows():
            mat = row.get("Materia", "N/A")
            cal = row.get("Calificacion", "N/A")
            lines.append(f"- {mat}: {cal}")
        texto = "\n".join(lines)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Ver datos generales", callback_data="general")],
            [InlineKeyboardButton("🔄 Consultar otra matrícula", callback_data="back")]
        ])
        return await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb)

    elif query.data == "general":
        resumen = (
            f"*{r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
            f"Carrera: {r.get('Carrera','N/A')}\n"
            f"Matrícula: {r.get('Matricula','N/A')}\n"
            f"Promedio general: {r.get('Calificacion','N/A')}\n"
            f"Cuatrimestre: {r.get('Cuatrimestre','N/A')}\n"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔢 Ver calificaciones", callback_data="grades")],
            [InlineKeyboardButton("🔄 Consultar otra matrícula", callback_data="back")]
        ])
        return await query.edit_message_text(resumen, parse_mode="Markdown", reply_markup=kb)

    elif query.data == "back":
        return await query.edit_message_text("✏️ Escribe la *matrícula* del alumno que deseas consultar.", parse_mode="Markdown")

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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buscar_matricula))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot arrancado correctamente.")
    app.run_polling()