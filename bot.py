import re
import random
import discord
from discord.ext import commands
import google.generativeai as genai
from groq import Groq
from datetime import datetime
import json
import os
from dotenv import load_dotenv
import asyncio
from collections import defaultdict
import time
from flask import Flask
from threading import Thread

# ================== CARREGA .ENV ==================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
ID_CANAL_STATUS = 1497646983287144568

# ================== INICIALIZAÇÃO DE APIs ==================
genai.configure(api_key=GEMINI_API_KEY)
groq_client = Groq(api_key=GROQ_API_KEY)

# Configuração global da pesquisa: 0 = Desativado, 1 = Normal, 2 = Avançado
modo_pesquisa = 1 

tavily_client = None
if TAVILY_API_KEY:
    try:
        from tavily import TavilyClient
        tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
        print("🔍 Tavily carregado com sucesso!")
    except ImportError:
        print("⚠️ Alerta: Pacote 'tavily-python' não instalado. Instale usando: pip install tavily-python")
else:
    print("⚠️ Alerta: TAVILY_API_KEY não encontrada no .env. Ferramenta de busca desativada.")

# Função de busca adaptável para o Gemini usar como ferramenta
def buscar_na_internet(query: str) -> str:
    """Busca informações em tempo real na internet para responder dúvidas atuais ou fatos recentes."""
    global modo_pesquisa
    if not tavily_client or modo_pesquisa == 0:
        return "Erro: Ferramenta de busca na internet desativada no momento."
    try:
        profundidade = "advanced" if modo_pesquisa == 2 else "basic"
        print(f"🌐 [Tavily - {profundidade.upper()}] Buscando por: '{query}'")
        
        # Realiza a busca no Tavily respeitando a profundidade escolhida
        resposta = tavily_client.get_search_context(query=query, search_depth=profundidade, max_results=3)
        return resposta
    except Exception as e:
        return f"Erro ao realizar busca na internet: {e}"

# --- SISTEMA DE DATABANK (JSON) ---
def carregar_json(caminho):
    if os.path.exists(caminho):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Erro ao ler {caminho}: {e}")
    return {}

def salvar_json(caminho, dados):
    try:
        with open(caminho, "w", encoding="utf-8") as f:
            json.dump(dados, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Erro ao salvar {caminho}: {e}")

DICIONARIO_PATH = "dicionario_hana.json"
MEMORIAS_INDIVIDUAIS_PATH = "memorias_usuarios.json" 

current_mode = "gemini" 

# ================== EMOJIS CUSTOMIZADOS ==================
EMOJIS = {
    "aura":   "<:hanaaura:1514023170775056394>",
    "heart":  "<:hanaheart:1514022538416623626>",
    "laugh":  "<:hanalaugh:1514024331095572561>",
    "rage":   "<:hanarage:1514024194327842936>",
    "soviet": "<:hanasoviet:1512515623668945086>",
    "think":  "<:hanathink:1523237178195316786>",
}

# ================== AUTOMOD ANTI-RAID ==================
AUTOMOD_JANELA_SEGUNDOS = 5
AUTOMOD_MSGS_MUTE = 5
AUTOMOD_CANAIS_KICK = 3
AUTOMOD_DURACAO_MUTE = 300

raid_tracker = defaultdict(lambda: {"timestamps": [], "canais": set()})

async def automod_check(message):
    if message.author.bot or not message.guild or message.author.guild_permissions.administrator:
        return

    user_id = message.author.id
    agora = time.time()
    dados = raid_tracker[user_id]

    dados["timestamps"] = [t for t in dados["timestamps"] if agora - t < AUTOMOD_JANELA_SEGUNDOS]

    if "canais_tempo" not in dados:
        dados["canais_tempo"] = []
    dados["canais_tempo"] = [(c, t) for c, t in dados["canais_tempo"] if agora - t < AUTOMOD_JANELA_SEGUNDOS]
    dados["canais"] = {c for c, t in dados["canais_tempo"]}

    dados["timestamps"].append(agora)
    dados["canais_tempo"].append((message.channel.id, agora))
    dados["canais"].add(message.channel.id)

    qtd_msgs = len(dados["timestamps"])
    qtd_canais = len(dados["canais"])

    if qtd_canais >= AUTOMOD_CANAIS_KICK:
        del raid_tracker[user_id]
        try:
            await message.author.kick(reason="[AutoMod Hana] Raid detectado: mensagens em múltiplos canais.")
            await log_status(f"🚨 **AUTOMOD - KICK:** {message.author} (ID: {message.author.id}) mandou msgs em **{qtd_canais} canais diferentes** em {AUTOMOD_JANELA_SEGUNDOS}s.")
            await message.channel.send(f"🚨 **{message.author.display_name}** foi kickado pelo AutoMod. Comportamento de raid detectado! {EMOJIS['rage']}")
            
            limite_tempo = discord.utils.utcnow() - __import__('datetime').timedelta(hours=12)
            apagadas = 0
            for canal in message.guild.text_channels:
                try:
                    msgs_usuario = [m async for m in canal.history(after=limite_tempo, limit=500) if m.author.id == message.author.id]
                    if msgs_usuario:
                        await canal.delete_messages(msgs_usuario)
                        apagadas += len(msgs_usuario)
                except (discord.Forbidden, discord.HTTPException):
                    pass
            if apagadas:
                await log_status(f"🧹 **AUTOMOD - PURGE:** {apagadas} mensagens das últimas 12h de {message.author} apagadas.")
        except discord.Forbidden:
            await log_status(f"⚠️ AutoMod tentou kickar {message.author} mas não tem permissão.")
        return

    if qtd_msgs >= AUTOMOD_MSGS_MUTE:
        del raid_tracker[user_id]
        try:
            duracao = discord.utils.utcnow() + __import__('datetime').timedelta(seconds=AUTOMOD_DURACAO_MUTE)
            await message.author.timeout(duracao, reason="[AutoMod Hana] Spam detectado.")
            await log_status(f"🔇 **AUTOMOD - MUTE:** {message.author} (ID: {message.author.id}) mandou **{qtd_msgs} msgs** em {AUTOMOD_JANELA_SEGUNDOS}s no mesmo canal.")
            await message.channel.send(f"🔇 **{message.author.display_name}** foi mutado por {AUTOMOD_DURACAO_MUTE // 60} minutos. Calminha no spam! {EMOJIS['rage']}")
        except discord.Forbidden:
            await log_status(f"⚠️ AutoMod tentou mutar {message.author} mas não tem permissão.")

# ================== LORE E PERSONALIDADE ==================
HANA_LORE = f"""
Você é a Hana, uma vtuber IA brincalhona e autêntica.
Personalidade: Casual, levemente debochada, brincalhona.
Regra de Ouro: Odeia flertes.
FIGURINHAS: {EMOJIS['aura']}, {EMOJIS['rage']}, {EMOJIS['heart']}, {EMOJIS['laugh']}, {EMOJIS['soviet']}, {EMOJIS['think']}.
"""

HANA_APPEARANCE = (
    "cute anime girl, masterpiece, official art, sparkling star-shaped pupils, "
    "heterochromia (red and blue eyes), white hair with red and blue streaks, "
    "red and blue star hair clips, black oversized t-shirt with 'HANACORD' text."
)

class HanaSession:
    def __init__(self):
        self.history = [] 
        self.interactions = 0
        self.last_interaction = datetime.now()

    def clean_history(self):
        if len(self.history) > 30:
            self.history = self.history[-30:]

# ================== CONFIGURAÇÃO DO DISCORD ==================
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
sessions = {}
startup_time = datetime.now()
CONFIG_PATH = "config_servidores.json"

def carregar_config(guild_id: int) -> dict:
    dados = carregar_json(CONFIG_PATH)
    return dados.get(str(guild_id), {})

def salvar_config(guild_id: int, config: dict):
    dados = carregar_json(CONFIG_PATH)
    dados[str(guild_id)] = config
    salvar_json(CONFIG_PATH, dados)

async def log_status(msg):
    canal = bot.get_channel(ID_CANAL_STATUS)
    if canal:
        timestamp = datetime.now().strftime("%H:%M:%S")
        await canal.send(f"🛰️ [`{timestamp}`] **Sistema Hana:** {msg}")

@bot.event
async def on_member_join(member):
    config = carregar_config(member.guild.id)
    canal_id = config.get("canal_boas_vindas", 1497982869790789795)
    canal = member.guild.get_channel(canal_id)
    if not canal: return

    mensagem = (
        f"➡️ **<@{member.id}>**\n"
        f"```ini\n"
        f"HANACORD.EXE // BOOT SEQUENCE INIT\n\n"
        f"[OK] HCD.system loaded.\n"
        f"[OK] Importing modules: events, chat, media...\n"
        f"[>>] Scanning for new users...\n"
        f"[>>] User detected: {member.display_name}\n"
        f"[>>] Fetching profile data...\n"
        f"[>>] Access level: GRANTED.\n"
        f"-----------------------------------------\n"
        f"STATUS: CONNECTION ESTABLISHED\n"
        f"-----------------------------------------\n"
        f"```\n"
        f"> Bem-vindo(a) ao apartamento da Hana.\n"
        f"> 🖥️ >>> Use `!ajuda` para ver os comandos disponíveis.\n"
        f"> 📑 >>> Leia as regras do servidor.\n\n"
        f"```python\n"
        f"if member.is_new():\n"
        f"    print(\"Hana diz: fique à vontade.\")\n"
        f"```\n"
        f"```ini\n"
        f"-----------------------------------------\n"
        f"HNC://ACCESS_GRANTED\n"
        f"-----------------------------------------\n"
        f"```\n"
        f">>> USER_ID=`\"{member.id}\"`"
    )
    await canal.send(mensagem)

@bot.event
async def on_member_remove(member):
    config = carregar_config(member.guild.id)
    canal_id = config.get("canal_boas_vindas", 1497982869790789795)
    canal = member.guild.get_channel(canal_id)
    if not canal: return

    mensagem = (
        f"⬅️ **{member.display_name}**\n"
        f"```ini\n"
        f"HANACORD.EXE // SESSION TERMINATED\n\n"
        f"[OK] Saving user data...\n"
        f"[OK] Closing active connections...\n"
        f"[>>] User: {member.display_name}\n"
        f"[>>] Status: DISCONNECTED\n"
        f"-----------------------------------------\n"
        f"STATUS: CONNECTION LOST\n"
        f"-----------------------------------------\n"
        f"```\n"
        f"> Até a próxima, {member.display_name}. Ou não.\n\n"
        f"```python\n"
        f"if member.left():\n"
        f"    print(\"Hana diz: a porta é ali.\")\n"
        f"```\n"
        f"```ini\n"
        f"-----------------------------------------\n"
        f"HNC://SESSION_CLOSED\n"
        f"-----------------------------------------\n"
        f"```\n"
        f">>> USER_ID=`\"{member.id}\"`"
    )
    await canal.send(mensagem)

@bot.event
async def on_ready():
    await bot.change_presence(status=discord.Status.idle, activity=discord.CustomActivity(name="Use !ajuda ✨"))
    await log_status(f"Hana **v4.7** ONLINE! Modo: {current_mode.upper()} | Latência: {round(bot.latency * 1000)}ms")
    print(f'✅ Hana v4.7 Online como {bot.user}')

@bot.event
async def on_message(message):
    global current_mode, modo_pesquisa
    if message.author == bot.user: return

    await automod_check(message)

    foi_marcado_diretamente = any(user.id == bot.user.id for user in message.mentions)

    if foi_marcado_diretamente or isinstance(message.channel, discord.DMChannel):
        user_id = str(message.author.id)
        user_display_name = message.author.display_name 
        
        if user_id not in sessions:
            sessions[user_id] = HanaSession()
        
        sn = sessions[user_id]
        sn.last_interaction = datetime.now()
        sn.clean_history()

        async with message.channel.typing():
            try:
                prompt = message.content.replace(f'<@{bot.user.id}>', '').strip()

                termos = carregar_json(DICIONARIO_PATH)
                todas_memorias = carregar_json(MEMORIAS_INDIVIDUAIS_PATH)
                memoria_pessoal = todas_memorias.get(user_id, "Nenhuma memória salva.")

                lore_completa = (
                    f"{HANA_LORE}\n"
                    f"ESTOU FALANDO COM: {user_display_name} (ID: {user_id})\n"
                    f"[MEMÓRIA LONGA DE {user_display_name}]: {memoria_pessoal}\n"
                    f"[DICIONÁRIO DO SERVIDOR]: {termos}\n"
                    f"DATA/HORA ATUAL: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
                )

                # Processamento de Imagem (Visão)
                if message.attachments:
                    if current_mode == "groq":
                        await message.reply(f"❌ O Llama 3.3 não consegue processar imagens. Troca pro Gemini com `!switch` se precisar disso! {EMOJIS['rage']}")
                        return
                    
                    model_v = genai.GenerativeModel("gemini-3.1-flash-lite")
                    att = message.attachments[0]
                    if any(att.filename.lower().endswith(ext) for ext in ['png', 'jpg', 'jpeg', 'webp']):
                        data = await att.read()
                        res = model_v.generate_content([lore_completa + "\nAnalise esta imagem pra mim.", {"mime_type": att.content_type, "data": data}])
                        await message.reply(res.text)
                        return

                # ==================== GEMINI ====================
                if current_mode == "gemini":
                    # Vincula a ferramenta apenas se o Tavily estiver configurado E não estiver desativado (modo 0)
                    ferramentas = [buscar_na_internet] if (tavily_client and modo_pesquisa != 0) else None
                    
                    model_g = genai.GenerativeModel(
                        "gemini-3.1-flash-lite",
                        tools=ferramentas
                    )
                    
                    # Converte o histórico guardado para o formato que o Gemini espera
                    chat_history = []
                    for m in sn.history:
                        role = "user" if m["role"] == "user" else "model"
                        chat_history.append({"role": role, "parts": [m["content"]]})
                    
                    # Inicia o chat enviando a lore + o prompt do usuário de uma vez
                    chat = model_g.start_chat(history=chat_history)
                    prompt_completo = f"{lore_completa}\nUsuário diz: {prompt}"
                    
                    response = chat.send_message(prompt_completo)
                    
                    # --- LOOP DE TOOL CALLING ---
                    # Se o Gemini decidir usar a ferramenta de busca, precisamos resolver as chamadas
                    # até que ele retorne um texto de fato.
                    while response.candidates and response.candidates[0].content.parts[0].function_call:
                        for part in response.candidates[0].content.parts:
                            if part.function_call:
                                # Executa a função localmente (Tavily)
                                resultado_busca = buscar_na_internet(part.function_call.args["query"])
                                # Envia o resultado de volta para o modelo continuar gerando o texto
                                response = chat.send_message(resultado_busca)
                    
                    resposta_texto = response.text

                # ==================== GROQ / LLAMA 3.3 70B ====================
                elif current_mode == "groq":
                    # Constrói o histórico para o Groq no formato messages
                    chat_history_groq = []
                    
                    # Adiciona o histórico da conversa
                    for m in sn.history:
                        chat_history_groq.append({
                            "role": "user" if m["role"] == "user" else "assistant",
                            "content": m["content"]
                        })
                    
                    # Prepara o prompt final com lore embutida
                    prompt_final_groq = f"{lore_completa}\n\nUsuário diz: {prompt}"
                    
                    # Faz a chamada ao Groq com Llama 3.3 70B
                    response_groq = groq_client.messages.create(
                        model="llama-3.3-70b-versatile",
                        messages=chat_history_groq + [{"role": "user", "content": prompt_final_groq}],
                        max_tokens=1024,
                        temperature=0.7
                    )
                    
                    resposta_texto = response_groq.content[0].text

                # Adiciona à memória e responde
                sn.history.append({"role": "user", "content": prompt})
                sn.history.append({"role": "assistant", "content": resposta_texto})
                sn.interactions += 1

                await message.reply(resposta_texto)

            except Exception as e:
                await log_status(f"ERRO DE PROCESSAMENTO: {e}")
                await message.reply(f"Meu cyberware fritou legal agora! {EMOJIS['rage']}")

    await bot.process_commands(message)

# ================== COMANDOS DE MODERAÇÃO ==================
@bot.command()
@commands.has_permissions(kick_members=True)
async def kick(ctx, member: discord.Member, *, motivo="Falta de aura."):
    await member.kick(reason=motivo)
    await log_status(f"👢 **KICK:** {member.name} (ID: {member.id}) por {ctx.author.name}. Motivo: {motivo}")
    await ctx.send(f"👢 **{member.display_name}** teve que dar no delta! Motivo: {motivo}")

@bot.command()
@commands.has_permissions(ban_members=True)
async def ban(ctx, member: discord.Member, *, motivo="Se graduou no banimento."):
    await member.ban(reason=motivo)
    await log_status(f"⚖️ **BAN:** {member.name} (ID: {member.id}) por {ctx.author.name}. Motivo: {motivo}")
    await ctx.send(f"⚖️ **{member.display_name}** se graduou e perdeu -1.000 de aura! {EMOJIS['rage']}")

@bot.command()
@commands.has_permissions(manage_messages=True)
async def clear(ctx, quantidade: int):
    if quantidade <= 0:
        return await ctx.send("Como vou limpar 0 mensagens? Ta bebendo água de ar condicionado?")
    deleted = await ctx.channel.purge(limit=quantidade + 1)
    await log_status(f"🧹 **CLEAR:** {len(deleted)-1} mensagens limpas por {ctx.author.name} em #{ctx.channel.name}")
    await ctx.send(f"🧹 Aura purificada! {len(deleted)-1} mensagens deletadas.", delete_after=5)

# ================== COMANDOS DE CÉREBRO E CONFIG ==================
@bot.command()
@commands.has_permissions(administrator=True)
async def switch(ctx):
    global current_mode
    current_mode = "groq" if current_mode == "gemini" else "gemini"
    await log_status(f"🧠 **SISTEMA:** Modelo trocado para {current_mode.upper()} por {ctx.author.name}")
    await ctx.send(f"🧠 **Switch!** Agora estou usando o cérebro: **{current_mode.upper()}**")

@bot.command()
async def modo(ctx):
    modelos = {
        "gemini": "Gemini 3.1 Flash Lite (com visão + busca web)",
        "groq": "Llama 3.3 70B Versatile (rápido e poderoso)"
    }
    await ctx.send(f"🧠 **Modelo Ativo:** {current_mode.upper()}\n> {modelos.get(current_mode, 'Desconhecido')}")

@bot.command()
@commands.has_permissions(administrator=True)
async def search(ctx, modo: int = None):
    """Configura a pesquisa web (0 = Off, 1 = Normal, 2 = Avançado)"""
    global modo_pesquisa
    if modo is None or modo not in [0, 1, 2]:
        return await ctx.send("Modo inválido! Use:\n`!search 1` (Pesquisa Normal)\n`!search 2` (Pesquisa Avançada)\n`!search 0` (Desativar)")
    
    modo_pesquisa = modo
    status_texto = {0: "DESATIVADA ❌", 1: "ATIVA (Modo Normal) 🌐", 2: "ATIVA (Modo Avançado 🔥)"}
    await log_status(f"⚙️ **PESQUISA:** Configuração de busca web alterada para: {status_texto[modo]} por {ctx.author.name}")
    await ctx.send(f"✅ **Busca Web atualizada:** Agora estou com a pesquisa {status_texto[modo]}")

@bot.command()
async def status(ctx):
    global modo_pesquisa
    latencia = round(bot.latency * 1000)
    status_busca = {0: "Desativado ❌", 1: "Normal 🌐", 2: "Avançado 🔥"}[modo_pesquisa]
    
    embed = discord.Embed(title="📊 Status da Hana", color=0x00FF00)
    embed.add_field(name="Ping", value=f"{latencia}ms", inline=True)
    embed.add_field(name="Modelo", value=current_mode.upper(), inline=True)
    embed.add_field(name="Servidores", value=len(bot.guilds), inline=True)
    embed.add_field(name="Busca Web (Tavily)", value=status_busca, inline=True)
    await ctx.send(embed=embed)

# ================== COMANDOS DE MEMÓRIA E DICIONÁRIO ==================
@bot.command()
async def lembrar(ctx, *, texto):
    user_id = str(ctx.author.id)
    dados = carregar_json(MEMORIAS_INDIVIDUAIS_PATH)
    if user_id not in dados: dados[user_id] = []
    
    data_formatada = datetime.now().strftime("%d/%m/%Y às %H:%M")
    dados[user_id].append(f"({data_formatada}): {texto}")
    salvar_json(MEMORIAS_INDIVIDUAIS_PATH, dados)
    await ctx.reply(f"✅ Já anotei aqui, {ctx.author.display_name}! Não esqueço mais.")

@bot.command()
async def fatos(ctx):
    user_id = str(ctx.author.id)
    dados = carregar_json(MEMORIAS_INDIVIDUAIS_PATH)
    memoria = dados.get(user_id, [])
    
    if not memoria:
        return await ctx.send("Minha cabeça está vazia sobre você. Tente `!lembrar` algo.")
    
    texto = f"🧠 **O que eu sei sobre {ctx.author.display_name}:**\n\n"
    for i, fato in enumerate(memoria, 1):
        texto += f"`{i}`. {fato}\n"
    await ctx.send(texto)

@bot.command()
async def deletar(ctx, numero: int):
    user_id = str(ctx.author.id)
    dados = carregar_json(MEMORIAS_INDIVIDUAIS_PATH)
    memoria = dados.get(user_id, [])
    
    if 1 <= numero <= len(memoria):
        deletado = memoria.pop(numero-1)
        dados[user_id] = memoria
        salvar_json(MEMORIAS_INDIVIDUAIS_PATH, dados)
        await ctx.send(f"✅ Ok, apaguei que: *{deletado}*")
    else:
        await ctx.send("Número inválido, neandertal.")

@bot.command()
async def definir(ctx, palavra: str = None, *, significado: str = None):
    dados = carregar_json(DICIONARIO_PATH)
    
    if palavra == "lista":
        if not dados:
            return await ctx.send("O dicionário está limpo.")
        
        linhas = [f"**{p}**: {s}" for p, s in dados.items()]
        paginas = []
        pagina_atual = ""
        for linha in linhas:
            if len(pagina_atual) + len(linha) + 1 > 4000:
                paginas.append(pagina_atual)
                pagina_atual = linha
            else:
                pagina_atual = pagina_atual + "\n" + linha if pagina_atual else linha
        if pagina_atual:
            paginas.append(pagina_atual)
        
        total = len(paginas)
        for i, conteudo in enumerate(paginas, 1):
            titulo = f"📚 Dicionário HanaCord" + (f" — Parte {i}/{total}" if total > 1 else "")
            embed = discord.Embed(title=titulo, description=conteudo, color=0x7000FF)
            await ctx.send(embed=embed)
        return
    
    if palavra and significado:
        dados[palavra.lower()] = significado
        salvar_json(DICIONARIO_PATH, dados)
        return await ctx.send(f"✅ Gíria **{palavra}** gravada no meu Databank!")
    
    if palavra:
        signi = dados.get(palavra.lower(), "Não faço ideia do que seja isso.")
        return await ctx.send(f"🔍 **{palavra}**: {signi}")

# ================== UTILITÁRIOS E DIVERSÃO ==================
@bot.command()
async def desenha(ctx, *, ideia):
    async with ctx.typing():
        tags = "masterpiece, anime style, 2d, high quality, vibrant"
        if any(w in ideia.lower() for w in ["você", "selfie", "hana", "tu"]):
            prompt_final = f"{tags}, {HANA_APPEARANCE}, {ideia}"
        else:
            prompt_final = f"{tags}, {ideia}"

        try:
            model_img = genai.GenerativeModel("gemini-3.1-flash-lite-image")
            response = model_img.generate_content(prompt_final)

            imagem_bytes = None
            for part in response.parts:
                if part.inline_data:
                    imagem_bytes = part.inline_data.data
                    break

            if not imagem_bytes:
                return await ctx.send(f"❌ Não consegui gerar a imagem agora... {EMOJIS['rage']}")

            caminho = "hana_desenho_temp.png"
            with open(caminho, "wb") as f:
                f.write(imagem_bytes)

            arquivo = discord.File(caminho, filename="hana_desenho.png")
            embed = discord.Embed(title="🎨 Minha Obra de Arte", color=0x7000FF)
            embed.set_image(url="attachment://hana_desenho.png")
            embed.set_footer(text=f"Prompt: {ideia}")
            await ctx.send(embed=embed, file=arquivo)

            os.remove(caminho)
        except Exception as e:
            print(f"Erro ao gerar imagem: {e}")
            await ctx.send(f"❌ Meu cyberware de desenho fritou! {EMOJIS['rage']}")

@bot.command()
async def aura(ctx, alvo: discord.Member = None):
    alvo = alvo or ctx.author
    valor = random.randint(0, 100)

    if valor == 100:
        msg = f"{EMOJIS['aura']} **{alvo.mention} farma muita aura!** {EMOJIS['aura']}\n> `AURA: {valor}%` — LENDÁRIO."
    elif valor >= 75:
        msg = f"{EMOJIS['aura']} **{alvo.mention}** tá bem de aura.\n> `AURA: {valor}%`"
    elif valor >= 50:
        msg = f"**{alvo.mention}** sobreviveu. Por enquanto.\n> `AURA: {valor}%`"
    elif valor >= 25:
        msg = f"**{alvo.mention}** tá na zona de risco de virar neandertal.\n> `AURA: {valor}%` {EMOJIS['laugh']}"
    elif valor == 0:
        msg = f"{EMOJIS['rage']} **{alvo.mention} anda farmando muita aura de neandertal...**\n> `AURA: {valor}%` — CRÍTICO."
    else:
        msg = f"**{alvo.mention}** tá precisando treinar mais.\n> `AURA: {valor}%` {EMOJIS['rage']}"

    await ctx.send(msg)

@bot.slash_command(name="aura", description="Rola a aura de alguém (0% a 100%)")
async def slash_aura(ctx: discord.ApplicationContext, alvo: discord.Member = None):
    await aura(ctx, alvo)

@bot.command()
async def d20(ctx):
    resultado = random.randint(1, 20)

    if resultado == 20:
        msg = f"🎲 O dado deu **20**! Crítico! {EMOJIS['aura']}"
    elif resultado == 1:
        msg = f"🎲 O dado deu **1**. Falha crítica. Que vergonha, {ctx.author.mention}. {EMOJIS['laugh']}"
    elif resultado >= 15:
        msg = f"🎲 O dado deu **{resultado}**. Bom resultado."
    elif resultado >= 8:
        msg = f"🎲 O dado deu **{resultado}**. Mediano, como esperado."
    else:
        msg = f"🎲 O dado deu **{resultado}**. Tá ruim pra você. {EMOJIS['rage']}"

    await ctx.send(msg)

@bot.slash_command(name="d20", description="Rola um dado de 20 lados")
async def slash_d20(ctx: discord.ApplicationContext):
    await d20(ctx)

@bot.command(name="path")
async def lifepath(ctx, alvo: discord.Member = None):
    alvo = alvo or ctx.author
    escolha = random.choice(["Nomad", "Street Kid", "Corpo"])

    descricoes = {
        "Nomad": f"{EMOJIS['aura']} **{alvo.mention}** rola um Life Path: **Nomad**.\n> Cresceu na estrada, longe das megacorps. Família é tudo — sangue ou não.",
        "Street Kid": f"{EMOJIS['laugh']} **{alvo.mention}** rola um Life Path: **Street Kid**.\n> Nasceu e se criou nas ruas. Conhece Night City melhor que a própria cara.",
        "Corpo": f"{EMOJIS['soviet']} **{alvo.mention}** rola um Life Path: **Corpo**.\n> Formado dentro de uma corporação. Ambição e traição no sangue.",
    }
    await ctx.send(descricoes[escolha])

@bot.slash_command(name="path", description="Sorteia um Life Path do universo Cyberpunk")
async def slash_path(ctx: discord.ApplicationContext, alvo: discord.Member = None):
    await lifepath(ctx, alvo)

@bot.command()
@commands.has_permissions(administrator=True)
async def automod(ctx):
    embed = discord.Embed(title="🛡️ AutoMod Hana — Configurações", color=0xFF4444)
    embed.add_field(name="⏱️ Janela de tempo", value=f"`{AUTOMOD_JANELA_SEGUNDOS}` segundos", inline=True)
    embed.add_field(name="🔇 Mute após", value=f"`{AUTOMOD_MSGS_MUTE}` msgs no mesmo canal", inline=True)
    embed.add_field(name="👢 Kick após", value=f"`{AUTOMOD_CANAIS_KICK}` canais diferentes", inline=True)
    embed.add_field(name="⏳ Duração do mute", value=f"`{AUTOMOD_DURACAO_MUTE // 60}` minutos", inline=True)
    embed.set_footer(text="Edite as constantes AUTOMOD_* no código pra ajustar.")
    await ctx.send(embed=embed)

@bot.command()
async def limpar(ctx):
    user_id = str(ctx.author.id)
    if user_id in sessions:
        sessions[user_id] = HanaSession()
        await ctx.send(f"Season resetada! Minha memória curta sobre você foi pro delta. {EMOJIS['aura']}")

@bot.command(name="ajuda")
async def ajuda(ctx):
    embed = discord.Embed(title="🤖 Central de Comando Hana 4.7", color=0x7000FF)
    embed.add_field(name="🛡️ Moderação", value="`!kick`, `!ban`, `!clear [N]`, `!automod` (ADM)", inline=False)
    embed.add_field(name="🧠 Memória Privada", value="`!lembrar`, `!fatos`, `!deletar [N]`", inline=True)
    embed.add_field(name="📚 Dicionário", value="`!definir [termo] [msg]`, `!definir lista`", inline=True)
    embed.add_field(name="⚙️ Config", value="`!switch` (ADM), `!search [0/1/2]` (ADM), `!modo`, `!status`, `!limpar`, `!prefixo`, `!setwelcome`", inline=False)
    embed.add_field(name="🌆 Servidor", value="`!serverinfo`, `!userinfo [@user]`, `!avatar`, `!uptime`", inline=True)
    embed.add_field(name="🎲 Diversão", value="`!aura [@user]`, `!d20`, `!path [@user]`, `!desenha [ideia]`", inline=True)
    embed.set_footer(text="Hana v4.7 — Todos os comandos disponíveis também como /slash")
    await ctx.send(embed=embed)

# ================== SERVIDOR & CONFIG ==================
@bot.command()
async def serverinfo(ctx):
    g = ctx.guild
    total_membros = g.member_count
    bots = sum(1 for m in g.members if m.bot)
    humanos = total_membros - bots
    online = sum(1 for m in g.members if m.status != discord.Status.offline and not m.bot)
    criado_em = g.created_at.strftime("%d/%m/%Y")
    boost_level = g.premium_tier
    boosts = g.premium_subscription_count
    canais_texto = len(g.text_channels)
    canais_voz = len(g.voice_channels)
    cargos = len(g.roles) - 1

    embed = discord.Embed(title=f"🌆 {g.name}", color=0x7000FF)
    embed.set_thumbnail(url=g.icon.url if g.icon else discord.Embed.Empty)
    embed.add_field(name="👤 Dono", value=g.owner.mention if g.owner else "N/A", inline=True)
    embed.add_field(name="🆔 ID", value=f"`{g.id}`", inline=True)
    embed.add_field(name="📅 Criado em", value=criado_em, inline=True)
    embed.add_field(name="👥 Membros", value=f"Total: **{total_membros}** | Humanos: **{humanos}** | Bots: **{bots}**", inline=False)
    embed.add_field(name="🟢 Online", value=f"**{online}** humanos agora", inline=True)
    embed.add_field(name="📢 Canais", value=f"💬 {canais_texto} texto | 🔊 {canais_voz} voz", inline=True)
    embed.add_field(name="🏷️ Cargos", value=f"**{cargos}**", inline=True)
    embed.add_field(name="🚀 Boost", value=f"Nível **{boost_level}** com **{boosts}** boosts", inline=True)
    embed.set_footer(text="HNC://SERVER_SCAN_COMPLETE")
    await ctx.send(embed=embed)

@bot.slash_command(name="serverinfo", description="Informações detalhadas do servidor")
async def slash_serverinfo(ctx: discord.ApplicationContext):
    await serverinfo(ctx)

@bot.command()
async def userinfo(ctx, membro: discord.Member = None):
    membro = membro or ctx.author
    entrou_em = membro.joined_at.strftime("%d/%m/%Y às %H:%M") if membro.joined_at else "Desconhecido"
    criado_em = membro.created_at.strftime("%d/%m/%Y")
    eh_adm = membro.guild_permissions.administrator

    embed = discord.Embed(title=f"🪪 Ficha de {membro.display_name}", color=0x7000FF)
    embed.set_thumbnail(url=membro.display_avatar.url)
    embed.add_field(name="🆔 ID", value=f"`{membro.id}`", inline=True)
    embed.add_field(name="🤖 Bot?", value="Sim" if membro.bot else "Não", inline=True)
    embed.add_field(name="🛡️ Adm", value="Sim" if eh_adm else "Não", inline=True)
    embed.add_field(name="📅 Conta criada em", value=criado_em, inline=True)
    embed.add_field(name="➡️ Entrou no servidor em", value=entrou_em, inline=True)
    embed.set_footer(text="HNC://USER_SCAN_COMPLETE")
    await ctx.send(embed=embed)

@bot.slash_command(name="userinfo", description="Mostra informações detalhadas de um usuário")
async def slash_userinfo(ctx: discord.ApplicationContext, membro: discord.Member = None):
    await userinfo(ctx, membro)

@bot.command()
async def avatar(ctx, membro: discord.Member = None):
    membro = membro or ctx.author
    embed = discord.Embed(title=f"🖼️ Avatar de {membro.display_name}", color=0x7000FF)
    embed.set_image(url=membro.display_avatar.url)
    embed.set_footer(text=f"ID: {membro.id}")
    await ctx.send(embed=embed)

@bot.slash_command(name="avatar", description="Mostra o avatar de um usuário em tamanho cheio")
async def slash_avatar(ctx: discord.ApplicationContext, membro: discord.Member = None):
    await avatar(ctx, membro)

@bot.command()
async def uptime(ctx):
    delta = datetime.now() - startup_time
    horas, resto = divmod(int(delta.total_seconds()), 3600)
    minutos, segundos = divmod(resto, 60)
    dias = delta.days
    horas = horas % 24

    embed = discord.Embed(title="⏱️ Uptime da Hana", color=0x7000FF)
    embed.add_field(name="Online há", value=f"**{dias}d {horas}h {minutos}m {segundos}s**", inline=False)
    embed.add_field(name="🏓 Latência", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.set_footer(text="HNC://SYSTEM_UPTIME")
    await ctx.send(embed=embed)

@bot.slash_command(name="uptime", description="Mostra há quanto tempo a Hana está online")
async def slash_uptime(ctx: discord.ApplicationContext):
    await uptime(ctx)

@bot.command()
@commands.has_permissions(administrator=True)
async def prefixo(ctx, novo: str = None):
    if not novo:
        return await ctx.send(f"O prefixo atual é `{bot.command_prefix}`. Use `!prefixo [novo]` pra trocar.")
    if len(novo) > 5:
        return await ctx.send("Prefixo muito longo, choom. Máximo 5 caracteres.")
    bot.command_prefix = novo
    await log_status(f"🔧 **PREFIXO:** Trocado para `{novo}` por {ctx.author.name} em {ctx.guild.name}")
    await ctx.send(f"✅ Prefixo updated para `{novo}` neste servidor!")

@bot.slash_command(name="prefixo", description="Muda o prefixo do bot (Apenas ADM)")
@commands.has_permissions(administrator=True)
async def slash_prefixo(ctx: discord.ApplicationContext, novo: str):
    await prefixo(ctx, novo)

@bot.command()
@commands.has_permissions(administrator=True)
async def setwelcome(ctx, canal: discord.TextChannel = None):
    canal = canal or ctx.channel
    config = carregar_config(ctx.guild.id)
    config["canal_boas_vindas"] = canal.id
    salvar_config(ctx.guild.id, config)
    await log_status(f"📌 **SETWELCOME:** Canal de boas-vindas de {ctx.guild.name} definido como #{canal.name} por {ctx.author.name}")
    await ctx.send(f"✅ Canal de boas-vindas e saída definido como {canal.mention}!")

@bot.slash_command(name="setwelcome", description="Define o canal de boas-vindas deste servidor (Apenas ADM)")
@commands.has_permissions(administrator=True)
async def slash_setwelcome(ctx: discord.ApplicationContext, canal: discord.TextChannel = None):
    await setwelcome(ctx, canal)

# ================== SLASH MIRRORS ==================
@bot.slash_command(name="ajuda", description="Painel de ajuda completo")
async def slash_ajuda(ctx: discord.ApplicationContext): await ajuda(ctx)

@bot.slash_command(name="status", description="Status técnico do bot")
async def slash_status(ctx: discord.ApplicationContext): await status(ctx)

@bot.slash_command(name="modo", description="Informa qual modelo de IA está ativo")
async def slash_modo(ctx: discord.ApplicationContext): await modo(ctx)

@bot.slash_command(name="switch", description="Alterna entre Gemini e Groq/Llama (Apenas ADM)")
@commands.has_permissions(administrator=True)
async def slash_switch(ctx: discord.ApplicationContext): await switch(ctx)

@bot.slash_command(name="search", description="Configura a pesquisa web (0 = Off, 1 = Normal, 2 = Avançado) (ADM)")
@commands.has_permissions(administrator=True)
async def slash_search(ctx: discord.ApplicationContext, modo: discord.Option(int, "Escolha o modo", choices=[0, 1, 2])): await search(ctx, modo=modo)

@bot.slash_command(name="limpar", description="Reseta sua memória curta com a Hana")
async def slash_limpar(ctx: discord.ApplicationContext): await limpar(ctx)

@bot.slash_command(name="lembrar", description="Salva um fato na sua memória longa")
async def slash_lembrar(ctx: discord.ApplicationContext, texto: str): await lembrar(ctx, texto=texto)

@bot.slash_command(name="fatos", description="Exibe suas memórias salvas")
async def slash_fatos(ctx: discord.ApplicationContext): await fatos(ctx)

@bot.slash_command(name="deletar", description="Deleta uma memória específica pelo número")
async def slash_deletar(ctx: discord.ApplicationContext, numero: int): await deletar(ctx, numero)

@bot.slash_command(name="definir", description="Gerencia o dicionário do servidor")
async def slash_definir(ctx: discord.ApplicationContext, palavra: str, significado: str = None): await definir(ctx, palavra, significado=significado)

@bot.slash_command(name="desenha", description="Gera uma imagem via Gemini (necessário estar no modo Gemini)")
async def slash_desenha(ctx: discord.ApplicationContext, ideia: str): await desenha(ctx, ideia=ideia)

@bot.slash_command(name="kick", description="Expulsa um membro do servidor")
@commands.has_permissions(kick_members=True)
async def slash_kick(ctx: discord.ApplicationContext, membro: discord.Member, motivo: str = "Falta de aura."): await kick(ctx, membro, motivo=motivo)

@bot.slash_command(name="ban", description="Bane um membro permanentemente")
@commands.has_permissions(ban_members=True)
async def slash_ban(ctx: discord.ApplicationContext, membro: discord.Member, motivo: str = "Se graduou no banimento."): await ban(ctx, membro, motivo=motivo)

@bot.slash_command(name="clear", description="Limpa N mensagens do canal")
@commands.has_permissions(manage_messages=True)
async def slash_clear(ctx: discord.ApplicationContext, quantidade: int): await clear(ctx, quantidade)

@bot.slash_command(name="automod", description="Exibe configurações do AutoMod (Apenas ADM)")
@commands.has_permissions(administrator=True)
async def slash_automod(ctx: discord.ApplicationContext): await automod(ctx)

# ================== TRATAMENTO DE ERROS ==================
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.reply(f"Calma aí, professor Hiro! Você não tem aura suficiente pra usar esse comando. {EMOJIS['rage']}")
    elif isinstance(error, commands.MemberNotFound):
        await ctx.reply("Não achei esse usuário no servidor.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Tá faltando coisa no comando! Use `!ajuda` pra ver como faz.")
    else:
        print(f"Erro ignorado: {error}")

# ================== KEEP-ALIVE (FLASK) ==================
# Servidor HTTP mínimo só pra dar sinal de vida pro monitor (UptimeRobot/etc)
# e o Render não deixar o Web Service dormir por "falta de atividade".
app_flask = Flask('')

@app_flask.route('/')
def home():
    return "Hana tá online e de olho em vocês. 👁️"

@app_flask.route('/health')
def health():
    return {"status": "ok", "latencia_ms": round(bot.latency * 1000) if bot.is_ready() else None}, 200

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app_flask.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

async def main():
    async with bot:
        await bot.start(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    keep_alive()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
