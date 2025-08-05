#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# ─── LIBRERÍAS ────────────────────────────────────────────────────
import os
import re
import json
import unicodedata
import logging
import pandas as pd
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, MessageHandler,
    ContextTypes, filters, CallbackContext
)
from openai import OpenAI

# ─── CONFIGURACIÓN ───────────────────────────────────────────────
load_dotenv()
TOKEN = os.getenv("TELEGRAM_TOKEN")
CSV_PATH = os.getenv("CSV_PATH")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MEMORY_FILE = "memory.json"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── CARGA Y PREPARACIÓN DE DATOS ────────────────────────────────
try:
    df = pd.read_csv(CSV_PATH, encoding="latin1")
    logger.info("CSV cargado. Columnas: %s", df.columns.tolist())
    
    # Preprocesamiento para análisis
    df['Nombre_Completo'] = df['Nombre'] + ' ' + df['Paterno'] + ' ' + df['Materno']
    
    # Convertir calificaciones a numérico
    df['Calificacion'] = pd.to_numeric(df['Calificacion'], errors='coerce')
    
    # Crear versión normalizada para búsquedas
    df['Busqueda'] = df['Nombre_Completo'].apply(lambda x: re.sub(r'[^\w\s]', '', x.lower()))
    df['Profesor_Norm'] = df['Profesor'].apply(lambda x: re.sub(r'[^\w\s]', '', str(x).lower()))
    
    # Agregar promedio general por alumno
    promedios = df.groupby('Matricula')['Calificacion'].mean().reset_index()
    promedios.columns = ['Matricula', 'Promedio_General']
    df = pd.merge(df, promedios, on='Matricula', how='left')
    
    # Preparar datos de profesores (CORRECCIÓN: USAR SOLO DATOS DE PROFESORES)
    profesores_df = df.groupby('Profesor').agg({
        'Materia': 'nunique',
        'Calificacion': 'mean'
    }).reset_index()
    profesores_df.columns = ['Profesor', 'Materias_Impartidas', 'Promedio_Calificaciones']
    
    logger.info("Datos preparados para análisis IA. %d registros", len(df))
    
except Exception as e:
    logger.error("Error al leer/preparar CSV: %s", e)
    df = pd.DataFrame()
    profesores_df = pd.DataFrame()

# ─── SISTEMA DE MEMORIA ──────────────────────────────────────────
class MemorySystem:
    def __init__(self, file_path=MEMORY_FILE):
        self.file_path = file_path
        self.memory = self.load_memory()
        
    def load_memory(self):
        try:
            if os.path.exists(self.file_path):
                with open(self.file_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error cargando memoria: {str(e)}")
        return {
            "conversaciones": {},
            "conocimiento": {
                "alumnos": {},
                "profesores": {},
                "materias": {},
                "carreras": {}
            }
        }
    
    def save_memory(self):
        try:
            with open(self.file_path, 'w', encoding='utf-8') as f:
                json.dump(self.memory, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Error guardando memoria: {str(e)}")
    
    def update_conversation(self, user_id, role, content):
        if str(user_id) not in self.memory["conversaciones"]:
            self.memory["conversaciones"][str(user_id)] = []
        
        if len(self.memory["conversaciones"][str(user_id)]) > 10:
            self.memory["conversaciones"][str(user_id)] = self.memory["conversaciones"][str(user_id)][-10:]
        
        self.memory["conversaciones"][str(user_id)].append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat()
        })
        self.save_memory()
    
    def get_conversation_history(self, user_id):
        return self.memory["conversaciones"].get(str(user_id), [])
    
    def update_knowledge(self, entity_type, entity_id, data):
        if entity_type not in self.memory["conocimiento"]:
            self.memory["conocimiento"][entity_type] = {}
        
        if entity_id not in self.memory["conocimiento"][entity_type]:
            self.memory["conocimiento"][entity_type][entity_id] = data
        else:
            self.memory["conocimiento"][entity_type][entity_id].update(data)
        
        self.save_memory()
    
    def get_knowledge(self, entity_type, entity_id):
        return self.memory["conocimiento"][entity_type].get(entity_id, {})
    
    def get_related_knowledge(self, query):
        related = {}
        for entity_type in self.memory["conocimiento"]:
            for entity_id, data in self.memory["conocimiento"][entity_type].items():
                if any(keyword in query.lower() for keyword in data.get("keywords", [])):
                    related[f"{entity_type}_{entity_id}"] = data
        return related

# Inicializar sistema de memoria
memory_system = MemorySystem()

# ─── CLIENTE OPENAI ─────────────────────────────────────────────
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ─── AYUDANTE: NORMALIZAR TEXTO ─────────────────────────────────
def quitar_acentos(texto: str) -> str:
    if not isinstance(texto, str):
        return texto
    texto = re.sub(r"[^\w\s]", "", texto)
    n = unicodedata.normalize("NFD", texto)
    return "".join(c for c in n if unicodedata.category(c) != "Mn")

# ─── BUSCAR PROFESOR ────────────────────────────────────────────
def buscar_profesor(nombre: str):
    nombre = quitar_acentos(nombre).lower().strip()
    if not nombre:
        return None
    
    # Buscar coincidencias exactas o parciales en PROFESORES
    mask = df['Profesor_Norm'].str.contains(nombre, na=False)
    resultados = df[mask]
    
    if resultados.empty:
        return None
    
    # Agrupar por profesor (CORRECCIÓN: USAR SOLO DATOS DE PROFESORES)
    profesor_info = resultados.groupby('Profesor').agg({
        'Materia': lambda x: list(set(x)),
        'Calificacion': 'mean',
        'Carrera': lambda x: list(set(x)),
        'Cuatrimestre': lambda x: list(set(x))
    }).reset_index()
    
    profesor_info.columns = ['Profesor', 'Materias', 'Promedio_Calificaciones', 'Carreras', 'Cuatrimestres']
    
    # Obtener cantidad de alumnos (sin detalles personales)
    profesor_info['Cantidad_Alumnos'] = resultados['Matricula'].nunique()
    
    return profesor_info

# ─── IA: PROCESAMIENTO DE CONSULTAS CON MEMORIA ─────────────────
async def procesar_consulta_ia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if df.empty:
        return "⚠️ Base de datos no disponible. Intente más tarde."
    
    if not client:
        return "🔴 Error: API Key de OpenAI no configurada"
    
    user_id = update.message.from_user.id
    consulta = update.message.text.strip()
    
    # Primero verificar si es una consulta directa de profesor
    if "profesor" in consulta.lower() or "docente" in consulta.lower():
        # Extraer nombre de profesor
        match = re.search(r'(profesor|docente)\s+([\w\s]+)', consulta, re.IGNORECASE)
        if match:
            nombre_profesor = match.group(2).strip()
            profesor_info = buscar_profesor(nombre_profesor)
            if profesor_info is not None:
                respuesta = []
                for _, row in profesor_info.iterrows():
                    respuesta.append(
                        f"👨‍🏫 *Profesor: {row['Profesor']}*\n"
                        f"📚 Materias: {', '.join(row['Materias'][:3])}{'...' if len(row['Materias']) > 3 else ''}\n"
                        f"🏫 Carreras: {', '.join(row['Carreras'][:2])}{'...' if len(row['Carreras']) > 2 else ''}\n"
                        f"⭐ Promedio calificaciones: {row['Promedio_Calificaciones']:.2f}\n"
                        f"👥 Alumnos: {row['Cantidad_Alumnos']}"
                    )
                return "\n\n".join(respuesta)
    
    # Si no es una consulta directa de profesor, usar IA
    memory_system.update_conversation(user_id, "user", consulta)
    historial = memory_system.get_conversation_history(user_id)
    conocimiento_relacionado = memory_system.get_related_knowledge(consulta)
    
    # Resumen estadístico
    resumen = (
        f"Dataset con {len(df)} registros. "
        f"Carreras: {df['Carrera'].nunique()}, "
        f"Materias: {df['Materia'].nunique()}, "
        f"Profesores: {df['Profesor'].nunique()}, "
        f"Alumnos: {df['Matricula'].nunique()}. "
        f"Calificaciones: Min {df['Calificacion'].min():.1f}, "
        f"Max {df['Calificacion'].max():.1f}, "
        f"Avg {df['Calificacion'].mean():.1f}."
    )
    
    # Ejemplos de datos
    ejemplos = []
    sample_data = df.sample(min(5, len(df)))
    for _, row in sample_data.iterrows():
        ejemplos.append(
            f"- Alumno: {row['Nombre_Completo']} | "
            f"Materia: {row['Materia']} ({row['Calificacion']}) | "
            f"Profesor: {row['Profesor']} | "
            f"Carrera: {row['Carrera']} | "
            f"Cuatri: {row['Cuatrimestre']}"
        )
    
    # Prompt optimizado
    system_prompt = (
        "Eres un asistente académico especializado en datos educativos. "
        "Datos importantes:\n"
        "1. Los alumnos tienen: Matrícula, Nombre (Nombre + Paterno + Materno), Carrera, Promedio\n"
        "2. Los profesores están en la columna 'Profesor' y se relacionan con materias y alumnos\n"
        "3. Cada registro representa un alumno en una materia con un profesor\n\n"
        f"Resumen estadístico:\n{resumen}\n\n"
        "Estructura de datos:\n"
        "- Carrera: Nombre completo de la carrera\n"
        "- Matricula: Identificador único del alumno\n"
        "- Nombre, Paterno, Materno: Componentes del nombre ALUMNO\n"
        "- Materia: Nombre completo de la materia\n"
        "- Calificacion: Valor numérico (0-10)\n"
        "- Cuatrimestre: Periodo académico\n"
        "- Profesor: Nombre del DOCENTE (columna específica para profesores)\n"
        "- Género: M/F\n\n"
        "Conocimiento relacionado:\n" +
        (json.dumps(conocimiento_relacionado, ensure_ascii=False) if conocimiento_relacionado else "Ninguno") +
        "\n\nHistorial:\n" +
        "\n".join([f"{msg['role']}: {msg['content']}" for msg in historial]) +
        "\n\nEjemplos de registros:\n" + 
        '\n'.join(ejemplos)
    )
    
    try:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": consulta}
        ]
        
        response = client.chat.completions.create(
            model="gpt-3.5-turbo-1106",
            messages=messages,
            temperature=0.3,
            max_tokens=800,
            top_p=0.9
        )
        respuesta = response.choices[0].message.content.strip()
        memory_system.update_conversation(user_id, "assistant", respuesta)
        
        # Extraer y guardar entidades
        try:
            self_update_prompt = (
                "Analiza la interacción y extrae entidades importantes: "
                "alumnos (por matrícula), profesores (por nombre), materias, carreras. "
                "Devuelve JSON con estructura: "
                "{'alumnos': {matricula: {data}}, 'profesores': {nombre: {data}}, 'materias': {nombre: {data}}, 'carreras': {nombre: {data}}}"
                f"\n\nConsulta: {consulta}\nRespuesta: {respuesta}"
            )
            
            update_response = client.chat.completions.create(
                model="gpt-3.5-turbo-1106",
                messages=[
                    {"role": "system", "content": "Eres un extractor de información especializado"},
                    {"role": "user", "content": self_update_prompt}
                ],
                temperature=0.1,
                max_tokens=500,
                response_format={"type": "json_object"}
            )
            entities = json.loads(update_response.choices[0].message.content)
            
            for entity_type, items in entities.items():
                for entity_id, data in items.items():
                    if 'keywords' not in data:
                        data['keywords'] = []
                    data['keywords'].extend(re.findall(r'\b\w+\b', entity_id.lower()))
                    data['keywords'].extend(re.findall(r'\b\w+\b', data.get('nombre', '').lower()))
                    memory_system.update_knowledge(entity_type, entity_id, data)
        except Exception as e:
            logger.error(f"Error actualizando conocimiento: {str(e)}")
        
        # Manejo de respuestas sin datos
        if "no tengo información" in respuesta.lower() or "no hay datos" in respuesta.lower():
            return f"🔍 No encontré datos para: '{consulta}'\n\n" \
                   "ℹ️ Prueba con:\n- Matrícula (ej: 23070045)\n- Nombre completo alumno\n- Nombre profesor\n- 'Lista de profesores'"
                   
        return respuesta
    
    except Exception as e:
        logger.error(f"Error en IA: {str(e)}")
        return "🔴 Error procesando la consulta. Intente reformular."

# ─── HANDLERS ────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    memory_system.update_conversation(user_id, "system", "Nueva conversación iniciada")
    
    await update.message.reply_text(
        "🎓 *Sistema Académico Inteligente*\n\n"
        "Puedes consultar sobre:\n"
        "👤 Alumnos (por matrícula o nombre completo)\n"
        "👨‍🏫 Profesores (por nombre)\n"
        "📚 Materias y carreras\n\n"
        "Ejemplos:\n"
        "- 23070045 (matrícula alumno)\n"
        "- José Aaron Castor Salinas (alumno)\n"
        "- Profesor Alicia Murillo\n"
        "- Lista de profesores\n"
        "- Promedio de Ética Profesional\n\n"
        "¡Pregunta lo que necesites!",
        parse_mode="Markdown"
    )

async def buscar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip()
    user_id = update.message.from_user.id
    
    # Comandos especiales para profesores
    if texto.lower() in ["profesores", "lista de profesores", "docentes"]:
        profesores = df["Profesor"].dropna().unique()
        if len(profesores) > 0:
            respuesta = "👨‍🏫 *Lista de profesores:*\n\n" + "\n".join(f"- {p}" for p in profesores[:20])
            if len(profesores) > 20:
                respuesta += f"\n\nY {len(profesores)-20} más..."
            await update.message.reply_text(respuesta, parse_mode="Markdown")
        else:
            await update.message.reply_text("No se encontraron profesores en la base de datos.")
        return
    
    # Búsqueda directa de profesor (CORRECCIÓN: SEPARADO DE ALUMNOS)
    if texto.lower().startswith(("profesor ", "docente ")):
        nombre_prof = texto.split(" ", 1)[1].strip()
        profesor_info = buscar_profesor(nombre_prof)
        if profesor_info is not None:
            respuesta = []
            for _, row in profesor_info.iterrows():
                respuesta.append(
                    f"👨‍🏫 *Profesor: {row['Profesor']}*\n"
                    f"📚 *Materias que imparte:*\n{', '.join(row['Materias'][:5])}"
                    f"{'...' if len(row['Materias']) > 5 else ''}\n"
                    f"⭐ *Promedio calificaciones:* {row['Promedio_Calificaciones']:.2f}\n"
                    f"👥 *Alumnos:* {row['Cantidad_Alumnos']}"
                )
            await update.message.reply_text("\n\n".join(respuesta), parse_mode="Markdown")
            return
        else:
            await update.message.reply_text(f"⚠️ No se encontró al profesor: {nombre_prof}")
            return
    
    # 1) Busca por matrícula (solo dígitos) - ALUMNOS
    if texto.isdigit():
        sub = df[df["Matricula"].astype(str).str.strip() == texto]
        if not sub.empty:
            r = sub.iloc[0]
            mat = str(r["Matricula"]).strip()
            context.user_data["matricula"] = mat

            resumen = (
                f"👤 *Alumno: {r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
                f"🎓 Carrera: {r.get('Carrera','N/A')}\n"
                f"🔢 Matrícula: {mat}\n"
                f"⭐ Promedio general: {r.get('Promedio_General','N/A'):.2f}\n"
                f"📅 Cuatrimestre: {r.get('Cuatrimestre','N/A')}"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊 Ver calificaciones", callback_data=f"grades|{mat}"),
                    InlineKeyboardButton("🔄 Consultar otro", callback_data="back")
                ]
            ])
            await update.message.reply_text(resumen, parse_mode="Markdown", reply_markup=kb)
            
            # Actualizar conocimiento
            memory_system.update_knowledge("alumnos", mat, {
                "nombre": f"{r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}",
                "carrera": r.get('Carrera','N/A'),
                "promedio": r.get('Promedio_General','N/A'),
                "keywords": [mat, r['Nombre'].lower(), r.get('Paterno','').lower()]
            })
            return
        else:
            respuesta = await procesar_consulta_ia(update, context)
            await update.message.reply_text(respuesta)
            return

    # 2) Busca por nombre completo (>= 3 palabras) - ALUMNOS
    partes = quitar_acentos(texto).lower().split()
    if len(partes) >= 3:
        materno = partes[-1]
        paterno = partes[-2]
        nombres = " ".join(partes[:-2])

        # Normalizar columnas
        nombres_norm   = df["Nombre"].astype(str).apply(lambda x: quitar_acentos(x.lower()))
        paternos_norm  = df["Paterno"].astype(str).apply(lambda x: quitar_acentos(x.lower()))
        maternos_norm  = df["Materno"].astype(str).apply(lambda x: quitar_acentos(x.lower()))

        mask = (
            nombres_norm.str.contains(nombres, na=False) &
            paternos_norm.str.contains(paterno, na=False) &
            maternos_norm.str.contains(materno, na=False)
        )
        sub = df[mask]

        if not sub.empty:
            r = sub.iloc[0]
            mat = str(r["Matricula"]).strip()
            context.user_data["matricula"] = mat

            resumen = (
                f"👤 *Alumno: {r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
                f"🎓 Carrera: {r.get('Carrera','N/A')}\n"
                f"🔢 Matrícula: {mat}\n"
                f"⭐ Promedio general: {r.get('Promedio_General','N/A'):.2f}\n"
                f"📅 Cuatrimestre: {r.get('Cuatrimestre','N/A')}"
            )
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("📊 Ver calificaciones", callback_data=f"grades|{mat}"),
                    InlineKeyboardButton("🔄 Consultar otro", callback_data="back")
                ]
            ])
            await update.message.reply_text(resumen, parse_mode="Markdown", reply_markup=kb)
            
            # Actualizar conocimiento
            memory_system.update_knowledge("alumnos", mat, {
                "nombre": f"{r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}",
                "carrera": r.get('Carrera','N/A'),
                "promedio": r.get('Promedio_General','N/A'),
                "keywords": [mat, r['Nombre'].lower(), r.get('Paterno','').lower()]
            })
            return
        else:
            respuesta = await procesar_consulta_ia(update, context)
            await update.message.reply_text(respuesta)
            return

    # 3) Consulta de IA con memoria
    respuesta = await procesar_consulta_ia(update, context)
    await update.message.reply_text(respuesta)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    # 1) “Volver al inicio”
    if data == "back":
        return await query.edit_message_text(
            "✏️ Escribe *matrícula* o *nombre completo* de alumno, o *profesor ...*:",
            parse_mode="Markdown"
        )

    # 2) Extraer acción y matrícula
    action, mat = data.split("|", 1)
    sub = df[df["Matricula"].astype(str).str.strip() == mat]
    if sub.empty:
        return await query.edit_message_text("❌ Matrícula no encontrada.")

    r = sub.iloc[0]

    # 3) Mostrar calificaciones (ALUMNOS)
    if action == "grades":
        lines = ["📊 *Calificaciones por materia:*"]
        for _, row in sub.iterrows():
            matname = row.get("Materia", "N/A")
            cal     = row.get("Calificacion", "N/A")
            profesor = row.get("Profesor", "N/A")
            lines.append(f"- {matname}: {cal} (Prof: {profesor})")
        texto = "\n".join(lines)
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("👤 Ver datos alumno", callback_data=f"general|{mat}"),
                InlineKeyboardButton("🔄 Consultar otro", callback_data="back")
            ]
        ])
        return await query.edit_message_text(texto, parse_mode="Markdown", reply_markup=kb)

    # 4) Volver a datos generales (ALUMNOS)
    if action == "general":
        resumen = (
            f"👤 *Alumno: {r['Nombre']} {r.get('Paterno','')} {r.get('Materno','')}*\n"
            f"🎓 Carrera: {r.get('Carrera','N/A')}\n"
            f"🔢 Matrícula: {mat}\n"
            f"⭐ Promedio general: {r.get('Promedio_General','N/A'):.2f}\n"
            f"📅 Cuatrimestre: {r.get('Cuatrimestre','N/A')}"
        )
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📊 Ver calificaciones", callback_data=f"grades|{mat}"),
                InlineKeyboardButton("🔄 Consultar otro", callback_data="back")
            ]
        ])
        return await query.edit_message_text(resumen, parse_mode="Markdown", reply_markup=kb)

# ─── EJECUCIÓN ────────────────────────────────────────────────────
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

    logger.info("Bot académico arrancado correctamente.")
    app.run_polling()

if __name__ == "__main__":
    main()