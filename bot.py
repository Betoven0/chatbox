#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ─── MONKEY PATCH para PTB 20.x–21.x ───
import importlib
try:
    _upd = importlib.import_module("telegram.ext._updater")
    Updater = _upd.Updater
    if hasattr(Updater, "__slots__"):
        slots = list(Updater.__slots__)
        if "_Updater__polling_cleanup_cb" not in slots:
            slots.append("_Updater__polling_cleanup_cb")
            Updater.__slots__ = tuple(slots)
except Exception:
    pass

# ─── LIBRERÍAS ───
import os
import re
import unicodedata
import logging
import pandas as pd
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, MessageHandler,
    ContextTypes, filters
)

# ─── CONFIGURACIÓN ───
load_dotenv()
TOKEN    = os.getenv("TELEGRAM_TOKEN")
CSV_PATH = os.getenv("CSV_PATH")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── CARGA DE CSV ───
try:
    df = pd.read_csv(CSV_PATH, encoding="latin1")
    logger.info("CSV cargado. Columnas: %s", df.columns.tolist())
except Exception as e:
    logger.error("Error al leer CSV: %s", e)
    df = pd.DataFrame()

# ─── AYUDANTE DE TEXTO ───
def quitar_acentos(texto: str) -> str:
    if not isinstance(texto, str):
        return texto
    texto = re.sub(r"[^\w\s]", "", texto)
    n = unicodedata.normalize("NFD", texto)
    return "".join(c for c in n if unicodedata.category(c) != "Mn")

# ─── HANDLERS ───
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✏️ Escribe la *matrícula* (dígitos) o el *nombre completo* "
        "(nombre(s) + paterno + materno) del alumno.",
        parse_mode="Markdown"
    )

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()

    # 1) Matrícula pura
    if texto.isdigit():
        sub = df[df["Matricula"].astype(str).str.strip() == texto]

    # 2) Nombre completo (>= 3 palabras)
    else:
        partes = quitar_acentos(texto).lower().split()
        if len(partes) < 3:
            return await update.message.reply_text(
                "⚠️ Para buscar por nombre escribe al menos:\n"
                "`Nombre(s) ApellidoPaterno ApellidoMaterno`",
                parse_mode="Markdown"
            )

        # Separamos materno, paterno y todo lo demás como nombre(s)
        materno = partes[-1]
        paterno = partes[-2]
        nombres = " ".join(partes[:-2])

        # Normalizamos columnas
        nombres_norm   = df["Nombre"].astype(str).apply(lambda x: quitar_acentos(x.lower()))
        paternos_norm  = df["Paterno"].astype(str).apply(lambda x: quitar_acentos(x.lower()))
        maternos_norm  = df["Materno"].astype(str).apply(lambda x: quitar_acentos(x.lower()))

        mask = (
            nombres_norm.str.contains(nombres, na=False) &
            paternos_norm.str.contains(paterno, na=False) &
            maternos_norm.str.contains(materno, na=False)
        )
        sub = df[mask]

        # 2a) Sugerencias si no hay coincidencia exacta
        if sub.empty:
            # Buscamos por coincidencia parcial (primeras 3 letras)
            mask_sugg = (
                nombres_norm.str.contains(nombres[:3], na=False) |
                paternos_norm.str.contains(paterno[:3], na=False) |
                maternos_norm.str.contains(materno[:3], na=False)
            )
            candidatos = df[mask_sugg][["Nombre","Paterno","Materno"]].drop_duplicates()
            if not candidatos.empty:
                sugerencias = "\n".join(
                    f"- {r['Nombre']} {r['Paterno']} {r['Materno']}"
                    for _, r in candidatos.iterrows()
                )[:5]
                return await update.message.reply_text(
                    "❌ No se encontró coincidencia exacta.\n\n"
                    "🔍 Quizás quisiste decir:\n"
                    f"{sugerencias}\n\n"
                    "✏️ Vuelve a intentar con matrícula o nombre completo.",
                    parse_mode="Markdown"
                )

    # 3) Si aún no hay resultados
    if sub.empty:
        return await update.message.reply_text(
            "❌ No se encontró ningún alumno.\n\n"
            "✏️ Escribe una matrícula válida o nombre completo correcto.",
            parse_mode="Markdown"
        )

    # 4) Mostrar datos generales + botones
    r   = sub.iloc[0]
    mat = str(r["Matricula"]).strip()
    context.user_data["matricula"] = mat

    resumen = (
        f"*{r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
        f"Carrera: {r.get('Carrera','N/A')}\n"
        f"Matrícula: {mat}\n"
        f"Promedio general: {r.get('Calificacion','N/A')}\n"
        f"Cuatrimestre: {r.get('Cuatrimestre','N/A')}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Ver calificaciones", callback_data=f"grades|{mat}")],
        [InlineKeyboardButton("🔄 Consultar otro alumno", callback_data="back")]
    ])
    await update.message.reply_text(resumen, parse_mode="Markdown", reply_markup=kb)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # Volver al inicio
    if data == "back":
        return await query.edit_message_text(
            "✏️ Escribe la *matrícula* o el *nombre completo* del alumno.",
            parse_mode="Markdown"
        )

    # Extraemos acción y matrícula
    action, mat = data.split("|", 1)
    sub = df[df["Matricula"].astype(str).str.strip() == mat]
    if sub.empty:
        return await query.edit_message_text("❌ Matrícula no encontrada.")

    r = sub.iloc[0]

    # Mostrar calificaciones con botón para volver a generales
    if action == "grades":
        lines = ["*Calificaciones por materia:*"]
        for _, row in sub.iterrows():
            matname = row.get("Materia", "N/A")
            cal     = row.get("Calificacion", "N/A")
            lines.append(f"- {matname}: {cal}")
        texto = "\n".join(lines)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📋 Ver datos generales", callback_data=f"general|{mat}")],
            [InlineKeyboardButton("🔄 Consultar otro alumno", callback_data="back")]
        ])
        return await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb)

    # Volver a datos generales
    if action == "general":
        resumen = (
            f"*{r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
            f"Carrera: {r.get('Carrera','N/A')}\n"
            f"Matrícula: {mat}\n"
            f"Promedio general: {r.get('Calificacion','N/A')}\n"
            f"Cuatrimestre: {r.get('Cuatrimestre','N/A')}"
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔢 Ver calificaciones", callback_data=f"grades|{mat}")],
            [InlineKeyboardButton("🔄 Consultar otro alumno", callback_data="back")]
        ])
        return await query.edit_message.reply_text(resumen, parse_mode="Markdown", reply_markup=kb)

# ─── MAIN ───
def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .read_timeout(30)
        .write_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, buscar))
    app.add_handler(CallbackQueryHandler(callback_handler))

    logger.info("Bot arrancado correctamente.")
    app.run_polling()

if __name__ == "__main__":
    main()