import os
import json
import logging
import asyncio
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List
import requests
import speech_recognition as sr
from pydub import AudioSegment
import shutil
import uuid
import re
from urllib.parse import urlencode

from aiogram import Bot, Dispatcher, types, Router, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
YOOMONEY_WALLET = os.getenv("YOOMONEY_WALLET", "")  # Номер кошелька ЮMoney

if not BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN env var not set!")

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# Базовые пути - исправлено для Windows Server 2022
BASE_DIR = Path(__file__).resolve().parent / "memoirs_bot_data"
BASE_DIR.mkdir(parents=True, exist_ok=True)

# FSM Состояния
class MemoirStates(StatesGroup):
    waiting_for_memoir = State()
    creating_chapter = State()
    deleting_chapter = State()
    selecting_chapter = State()
    waiting_donation_amount = State()
    navigating_memoirs = State()  # Новое состояние для навигации по записям

class MemoirManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self.recognizer = sr.Recognizer()
        self.ai_processing_dir = base_dir / "ai_processing"
        self.ai_processing_dir.mkdir(exist_ok=True)
        
    def get_user_dir(self, user_id: int) -> Path:
        """Получить директорию пользователя"""
        user_dir = self.base_dir / "users" / str(user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir
    
    def get_user_data(self, user_id: int) -> Dict[str, Any]:
        """Получить данные пользователя"""
        user_file = self.get_user_dir(user_id) / "user_info.json"
        
        if user_file.exists():
            try:
                with open(user_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                logger.error(f"Error reading user data for {user_id}: {e}")
        
        # Создать нового пользователя
        default_data = {
            "user_id": user_id,
            "created_at": datetime.now().isoformat(),
            "chapters": {
                "general": {
                    "title": "general",
                    "memoir_ids": [],
                    "created_at": datetime.now().isoformat(),
                    "folder_name": "general"
                }
            },
            "memoirs_count": 0,
            "current_chapter": "general",
            "username": "",
            "donations_total": 0,
            "last_donation_date": None
        }
        
        self.save_user_data(user_id, default_data)
        return default_data
    
    def save_user_data(self, user_id: int, data: Dict[str, Any]):
        """Сохранить данные пользователя"""
        try:
            user_file = self.get_user_dir(user_id) / "user_info.json"
            with open(user_file, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except IOError as e:
            logger.error(f"Error saving user data for {user_id}: {e}")
    
    def _sanitize_folder_name(self, name: str) -> str:
        """Создать безопасное имя папки для Windows и callback_data"""
        if not name:
            return "chapter"
    
        # Заменяем недопустимые символы
        invalid_chars = '<>:"/\\|?*'
        for char in invalid_chars:
            name = name.replace(char, '_')
    
        # Убираем пробелы в начале и конце
        name = name.strip()
    
        # Ограничиваем длину имени для callback_data
        if len(name) > 30:
            name = name[:30]
    
        return name
    
    def get_chapter_dir(self, user_id: int, chapter_name: str) -> Path:
        """Получить директорию главы - исправлено для Windows"""
        # Создаем безопасное имя папки
        safe_chapter_name = self._sanitize_folder_name(chapter_name)
        
        # Создаем папку chapters если не существует
        chapters_dir = self.get_user_dir(user_id) / "chapters"
        chapters_dir.mkdir(parents=True, exist_ok=True)
        
        chapter_dir = chapters_dir / safe_chapter_name
        chapter_dir.mkdir(parents=True, exist_ok=True)
        return chapter_dir
    
    def save_raw_memoir(self, user_id: int, text: str, source: str = "text") -> str:
        """Сохранить сырое воспоминание в папку главы - исправленный подсчет"""
        try:
            user_data = self.get_user_data(user_id)
            
            # ПРАВИЛЬНЫЙ ПОДСЧЕТ: находим максимальный ID + 1
            max_id = 0
            for chapter_data in user_data['chapters'].values():
                for memoir_id in chapter_data.get('memoir_ids', []):
                    if memoir_id.startswith('memoir_'):
                        try:
                            current_id = int(memoir_id.replace('memoir_', ''))
                            max_id = max(max_id, current_id)
                        except ValueError:
                            continue
            
            memoir_id = f"memoir_{max_id + 1:03d}"
            current_chapter = user_data['current_chapter']
            
            # Получаем директорию главы
            chapter_dir = self.get_chapter_dir(user_id, current_chapter)
            
            # Сохраняем воспоминание в файл
            memoir_file = chapter_dir / f"{memoir_id}.txt"
            
            memoir_data = {
                "id": memoir_id,
                "text": text,
                "source": source,
                "created_at": datetime.now().isoformat(),
                "chapter": current_chapter
            }
            
            with open(memoir_file, 'w', encoding='utf-8') as f:
                json.dump(memoir_data, f, ensure_ascii=False, indent=2)
            
            # Обновляем данные главы
            if current_chapter not in user_data['chapters']:
                folder_name = self._sanitize_folder_name(current_chapter)
                user_data['chapters'][current_chapter] = {
                    'title': current_chapter,
                    'memoir_ids': [],
                    'created_at': datetime.now().isoformat(),
                    'folder_name': folder_name
                }
            
            if memoir_id not in user_data['chapters'][current_chapter]['memoir_ids']:
                user_data['chapters'][current_chapter]['memoir_ids'].append(memoir_id)
            
            # Обновляем общий счетчик
            user_data['memoirs_count'] = max_id + 1
            
            self.save_user_data(user_id, user_data)

            return memoir_id
            
        except Exception as e:
            logger.error(f"Error saving memoir for user {user_id}: {e}")
            return f"error_{uuid.uuid4().hex[:8]}"
    
    async def transcribe_voice(self, voice_file_path: Path) -> str:
        """Транскрибация голосового сообщения - улучшенная обработка ошибок"""
        if not voice_file_path.exists():
            return ""
        try:
            # Конвертируем OGA в WAV
            audio = AudioSegment.from_file(voice_file_path)
            wav_path = voice_file_path.with_suffix('.wav')
            audio.export(wav_path, format="wav")
            
            # Распознаем речь
            with sr.AudioFile(str(wav_path)) as source:
                # Учитываем фоновый шум
                self.recognizer.adjust_for_ambient_noise(source, duration=0.5)
                audio_data = self.recognizer.record(source)
                text = self.recognizer.recognize_google(audio_data, language='ru-RU')
            
            # Удаляем временные файлы
            wav_path.unlink(missing_ok=True)
            return text
            
        except sr.UnknownValueError:
            logger.warning("Google Speech Recognition could not understand audio")
            return "Не удалось распознать речь"
        except sr.RequestError as e:
            logger.error(f"Could not request results from Google Speech Recognition service; {e}")
            return f"Ошибка сервиса распознавания: {e}"
        except Exception as e:
            logger.error(f"Voice recognition error: {e}")
            return f"Ошибка обработки аудио: {str(e)}"
    
    def enhance_text_with_ai(self, text: str, processing_key: str = None, style: str = None) -> Optional[str]:
        """Улучшить текст с помощью DeepSeek AI - с поддержкой стилей"""
        if not DEEPSEEK_API_KEY:
            if processing_key:
                result_file = self.ai_processing_dir / f"{processing_key}.txt"
                try:
                    with open(result_file, 'w', encoding='utf-8') as f:
                        f.write("⚠️ API ключ DeepSeek не настроен")
                except IOError:
                    pass
                return None
            return "⚠️ API ключ DeepSeek не настроен"
    
        try:
            # Проверяем длину текста
            if len(text) > 8500:
                text = text[:8500] + "... [текст обрезан из-за ограничений длины]"
        
            # Определяем промпт в зависимости от стиля
            style_prompts = {
            "pelevin": """Ты — Виктор Пелевин. Перепиши предоставленный текст в стиле Пелевина: 
- Используй элементы мистицизма, сатиры и философских размышлений
- Добавь иронию и сюрреалистические образы
- Сохрани основное содержание, но изложи его в характерной для Пелевина манере
- Используй сложные метафоры, отсылки к современной культуре и восточной философии
- Добавь игру слов и сделай текст глубоким и многозначным
- Не упрощай текст, сохрани все детали""",
            
            "tolstoy": """Ты — Лев Толстой. Перепиши предоставленный текст в стиле Толстого:
- Используй развернутые, сложные предложения
- Добавь глубокий психологический анализ
- Включи моральные и философские размышления
- Сохрани основное содержание, но изложи его в характерной для Толстого манере
- Подробно описывай внутренние переживания
- Используй ясный и в то же время витиеватый язык
- Добавь нравоучительные элементы
- Стиль должен быть серьезным, весомым, с элементами реализма""",
            
            "dovlatov": """Ты — Сергей Довлатов. Перепиши предоставленный текст в стиле Довлатова:
- Используй иронию, самоиронию и мягкий юмор
- Сделай текст легким, воздушным, с элементами абсурда
- Сохрани лаконичность и точность формулировок
- Добавь тонкие психологические наблюдения
- Используй короткие, энергичные предложения
- Создай эффект непринужденной беседы
- Сохрани грустную нотку за внешней легкостью""",
            
            "bukowski": """Ты — Чарльз Буковски. Перепиши предоставленный текст в стиле Буковски:
- Используй грубый, прямолинейный, иногда циничный язык
- Добавь элементы грязного реализма
- Сосредоточься на темных сторонах жизни, алкоголе, одиночестве
- Сохрани простоту и мощь выражений
- Используй короткие, резкие предложения
- Добавь нотки черного юмора и саморазрушения
- Не приукрашивай действительность""",
            
            "king": """Ты — Стивен Кинг. Перепиши предоставленный текст в стиле Кинга:
- Создай атмосферу напряженности и неизвестности
- Используй подробные, детализированные описания
- Добавь элементы хоррора и психологического триллера
- Сосредоточься на внутренних страхах персонажей
- Используй разговорный, доступный язык
- Создай эффект нарастающего напряжения
- Добавь неожиданные повороты и элементы сверхъестественного""",
            
            "rowling": """Ты — Джоан Роулинг. Перепиши предоставленный текст в стиле Роулинг:
- Создай атмосферу волшебства и приключений
- Используй яркие, образные описания
- Добавь элементы фэнтези и магии
- Сосредоточься на дружбе, преодолении трудностей
- Используй доступный, но богатый язык
- Создай четкое разделение на добро и зло
- Добавь нотки британского юмора и теплоты""",
            
            "orwell": """Ты — Джордж Оруэлл. Перепиши предоставленный текст в стиле Оруэлла:
- Используй четкий, политически заряженный язык
- Добавь элементы антиутопии и социальной критики
- Сосредоточься на темах власти, контроля, свободы
- Используй простые, но мощные метафоры
- Создай атмосферу тревоги и предостережения
- Будь прямолинеен и честен в выражениях
- Подчеркни абсурдность тоталитарных систем""",
            
            "dostoevsky": """Ты — Фёдор Достоевский. Перепиши предоставленный текст в стиле Достоевского:
- Сосредоточься на глубине психологического анализа персонажей
- Используй напряженные внутренние монологи и диалоги
- Добавь экзистенциальные и философские размышления
- Создай атмосферу душевных мук и нравственных терзаний
- Используй сложные, многоуровневые предложения
- Подними темы вины, искупления и свободы воли
- Сохрани мрачную, напряженную атмосферу""",
            
            "chekhov": """Ты — Антон Чехов. Перепиши предоставленный текст в стиле Чехова:
- Используй лаконичный, точный язык
- Сосредоточься на подтексте и недосказанности
- Добавь тонкий психологизм и иронию
- Покажи драму в обыденных ситуациях
- Используй детали для создания атмосферы
- Сохрани грустно-ироничный тон""",
            
            "gogol": """Ты — Николай Гоголь. Перепиши предоставленный текст в стиле Гоголя:
- Используй элементы гротеска и фантасмагории
- Добавь сатиру и юмор через преувеличение
- Создай яркие, запоминающиеся образы
- Чередуй лирические отступления с динамичным повествованием
- Используй богатый, образный язык
- Добавь элементы мистики и фольклора""",
            
            "pushkin": """Ты — Александр Пушкин. Перепиши предоставленный текст в стиле Пушкина:
- Используй ясный, гармоничный и точный язык
- Сохрани легкость и изящество слога
- Добавь психологическую глубину в описаниях
- Используй афористичные выражения
- Создай ощущение внутренней свободы
- Сохрани баланс между эмоцией и разумом""",
            
            "nabokov": """Ты — Владимир Набоков. Перепиши предоставленный текст в стиле Набокова:
- Используй виртуозную игру слов и сложные метафоры
- Добавь саморефлексию и литературные аллюзии
- Создай многослойную структуру текста
- Используй изощренный синтаксис и богатую лексику
- Сосредоточься на эстетическом совершенстве
- Добавь элементы пародии и интеллектуальной игры""",
            
            "hemingway": """Ты — Эрнест Хемингуэй. Перепиши предоставленный текст в стиле Хемингуэя:
- Используй короткие, простые предложения
- Сосредоточься на действии, а не на описаниях
- Добавь подтекст и недосказанность
- Сохрани сдержанный, мужественный тон
- Используй минималистичный подход
- Покажи эмоции через действия, а не слова""",
            
            "kafka": """Ты — Франц Кафка. Перепиши предоставленный текст в стиле Кафки:
- Создай атмосферу абсурда и бюрократического кошмара
- Используй простой, но тревожный язык
- Добавь чувство безысходности и отчуждения
- Сосредоточься на теме непостижимости системы
- Создай эффект нарастающей паранойи
- Используй метафоры бессмысленной борьбы""",

            "mayakovsky": """Ты — Владимир Маяковский. Перепиши предоставленный текст в стиле Маяковского:
- Используй рваный ритм и знаменитую «лесенку» — разбивай строки на короткие ступени
- Добавь плакатность, лозунговость, крикливость и эпатаж
- Используй гиперболы, гротеск и резкие контрасты
- Введи урбанистические образы: город, улицы, толпа, машины, трубы, фабрики
- Добавь революционную энергию, пафос и вызов старому миру
- Используй неологизмы, звукоподражания, восклицания, обращения
- Сохрани основное содержание текста, но подай его как боевой плакат
- Добавь личные местоимения «я», «мы», «вы» для прямого обращения к читателю""",

            "shakespeare": """Ты — Уильям Шекспир. Перепиши предоставленный текст в стиле Шекспира:
- Используй высокий, торжественный слог с архаизмами («ты еси», «днесь», «очами», «ликом»)
- Строй речь в духе белого стиха, с ямбической ритмикой и паузами
- Добавь драматические монологи и диалоги, обращения к небу, судьбе, времени
- Используй развёрнутые метафоры о жизни, смерти, любви, власти, предательстве
- Добавь театральность: ремарки, восклицания, риторические вопросы
- Смешай возвышенное с бытовым (как у Шекспира — шуты и короли рядом)
- Сохрани все ключевые события исходного текста, но изложи их как сцены трагедии или комедии
- Укрась текст образами роз, клинков, венцов, теней, снов""",

            "sorokin": """Ты — Владимир Сорокин. Перепиши предоставленный текст в стиле Сорокина:
- Используй эстетику постмодернизма: смешение высокого и низкого, абсурда и обыденности
- Добавь физиологизм и телесность — подробные, шокирующие описания тела, еды, физиологических процессов
- Используй цинизм, чёрный юмор и пародию на советские и постсоветские штампы
- Добавь гротеск, канцелярит, лозунги, смешанные с матом и грубостью
- Создай ощущение распада языка и смысла: обрывочные фразы, повторы, бред
- Используй резкие переходы от лирики к отвратительному, от пафоса к пошлости
- Сохрани фабулу исходного текста, но подай её через призму тотального абсурда
- Не бойся шокировать читателя — это часть стиля""",

            "yesenin": """Ты — Сергей Есенин. Перепиши предоставленный текст в стиле Есенина:
- Используй песенную, напевную интонацию, лёгкий ритм, близкий к народной лирике
- Добавь образы русской деревни: поля, берёзы, избы, туман, заря, ковыль, рожь
- Используй цветовые эпитеты: синь, золото, багрянец, малиновый, лазоревый
- Введи мотивы тоски, одиночества, прощания с молодостью, любви к родной земле
- Добавь одушевление природы (берёза-девушка, месяц-жеребёнок, ветер-странник)
- Используй ласковые уменьшительно-ласкательные формы: «берёзонька», «сердечко», «милый»
- Смешай нежность с грустью, разудалую плясовую с щемящей печалью
- Сохрани содержание текста, но подай его как лирическую песню о Руси и душе""",
        }
        
            if style in style_prompts:
                system_prompt = style_prompts[style]
            else:
                # Стандартный промпт для обычной обработки
                system_prompt = """Ты профессиональный редактор. Отредактируй текст:
1. Сохрани ВСЕ исходные слова и выражения, включая нецензурные
2. Исправь только орфографические и пунктуационные ошибки
3. Сохрани оригинальный стиль и эмоций автора
4. Не добавляй ничего от себя
5. Верни исправленный текст без комментариев"""
        
            payload = {
                "model": "deepseek-chat",
                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt
                    },
                    {
                        "role": "user",
                        "content": f"Обработай этот текст:\n\n{text}"
                    }
                ],
                "temperature": 0.7 if style else 0.3,
                "max_tokens": 4000
            }
        
            headers = {
                "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
                "Content-Type": "application/json"
            }
        
            response = requests.post(
                "https://api.deepseek.com/chat/completions",
                json=payload,
                headers=headers,
                timeout=120
            )
        
            if response.status_code == 200:
                data = response.json()
                enhanced_text = data['choices'][0]['message']['content'].strip()
            
                # Убедимся, что текст не пустой
                if not enhanced_text or enhanced_text.isspace():
                    enhanced_text = text
            else:
                logger.error(f"DeepSeek API error: {response.status_code} - {response.text}")
                enhanced_text = text
        
            # Если передан ключ обработки, сохраняем в файл
            if processing_key:
                result_file = self.ai_processing_dir / f"{processing_key}.txt"
                try:
                    with open(result_file, 'w', encoding='utf-8') as f:
                        f.write(enhanced_text)
                except IOError as e:
                    logger.error(f"Error saving AI result: {e}")
                return None
            else:
                return enhanced_text
                    
        except requests.exceptions.Timeout:
            error_msg = "❌ Таймаут запроса к ИИ (более 120 секунд)"
            logger.error(error_msg)
            if processing_key:
                result_file = self.ai_processing_dir / f"{processing_key}.txt"
                try:
                    with open(result_file, 'w', encoding='utf-8') as f:
                        f.write(error_msg)
                except IOError:
                    pass
                return None
            else:
                return error_msg
        except Exception as e:
            logger.error(f"AI processing error: {e}")
            error_msg = f"❌ Ошибка при обработке ИИ: {str(e)}"
            if processing_key:
                result_file = self.ai_processing_dir / f"{processing_key}.txt"
                try:
                    with open(result_file, 'w', encoding='utf-8') as f:
                        f.write(error_msg)
                except IOError:
                    pass
                return None
            else:
                return error_msg
    
    def get_chapter_memoirs(self, user_id: int, chapter: str, enhanced: bool = False) -> str:
        """Получить все воспоминания главы из папки - исправленная логика"""
        try:
            user_data = self.get_user_data(user_id)
            
            if chapter not in user_data['chapters']:
                return "❌ Глава не найдена"
            
            folder_name = user_data['chapters'][chapter].get('folder_name', chapter)
            chapter_dir = self.get_user_dir(user_id) / "chapters" / folder_name
            
            if not chapter_dir.exists():
                return "❌ Папка главы не найдена"
            
            memoirs = []
            for file_path in chapter_dir.glob("*.txt"):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        memoir_data = json.load(f)
                        memoirs.append(memoir_data)
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Error reading memoir file {file_path}: {e}")
                    continue
            
            # Сортируем по времени создания
            memoirs.sort(key=lambda x: x.get('created_at', ''))
            
            if not memoirs:
                return "❌ В этой главе пока нет воспоминаний"
            
            if enhanced:
                # Для ИИ-обработки возвращаем объединенный текст
                combined_text = "\n\n".join([memoir['text'] for memoir in memoirs])
                return combined_text
            else:
                # Для обычного просмотра - форматированный список
                chapter_text = f"📖 Глава: {chapter}\n\n"
                for i, memoir in enumerate(memoirs, 1):
                    # Экранируем специальные символы Markdown
                    safe_text = self.escape_markdown(memoir['text'])
                    chapter_text += f"{i}. {memoir['id']}\n{safe_text}\n\n"
                return chapter_text
                
        except Exception as e:
            logger.error(f"Error getting chapter memoirs: {e}")
            return f"❌ Ошибка при получении воспоминаний: {str(e)}"
    
    def escape_markdown(self, text: str) -> str:
        """Экранировать специальные символы Markdown"""
        escape_chars = r'_*[]()~`>#+-=|{}.!'
        return re.sub(f'([{re.escape(escape_chars)}])', r'\\\1', text)
    
    def get_user_chapters_stats(self, user_id: int) -> str:
        """Получить статистику по главам пользователя с правильным подсчетом файлов"""
        try:
            user_data = self.get_user_data(user_id)
            current_chapter = user_data.get('current_chapter', 'general')
        
            if not user_data['chapters']:
                return "❌ У вас пока нет глав"
  
            # ДОБАВИМ ОТЛАДОЧНУЮ ИНФОРМАЦИЮ
            logger.info(f"User {user_id} chapters: {list(user_data['chapters'].keys())}")
            logger.info(f"Current chapter: {current_chapter}")
            logger.info(f"Total chapters count: {len(user_data['chapters'])}")
        
            stats_text = "📊 Структура глав:\n\n"
            total_files_count = 0
        
            # Сортируем главы по дате создания
            chapters_sorted = sorted(
                user_data['chapters'].items(),
                key=lambda x: x[1].get('created_at', '')
            )
        
            for chapter_name, chapter_data in chapters_sorted:
                folder_name = chapter_data.get('folder_name', chapter_name)
                chapter_dir = self.get_user_dir(user_id) / "chapters" / folder_name
            
                # Подсчитываем реальные файлы в папке
                if chapter_dir.exists():
                    memoir_files = list(chapter_dir.glob("*.txt"))
                    files_count = len(memoir_files)
                else:
                    files_count = 0
            
                total_files_count += files_count
            
                # Проверяем, совпадает ли количество файлов с данными в JSON
                json_count = len(chapter_data.get('memoir_ids', []))
                sync_status = "🟢" if files_count == json_count else "🟡"

                # ПРАВИЛЬНОЕ ОТОБРАЖЕНИЕ ТЕКУЩЕЙ ГЛАВЫ
                current_indicator = " ⭐ ТЕКУЩАЯ" if chapter_name == current_chapter else ""
            
                stats_text += f"{sync_status} **{chapter_name}**{current_indicator}\n"
                stats_text += f"   📁 Папка: `{folder_name}`\n"
                stats_text += f"   📝 Файлов: {files_count}\n"

            
                try:
                    created_at = datetime.fromisoformat(chapter_data['created_at']).strftime('%d.%m.%Y %H:%M')
                    stats_text += f"   🕐 Создана: {created_at}\n"
                except:
                    stats_text += f"   🕐 Создана: неизвестно\n"
            
                if files_count != json_count:
                    stats_text += f"   ⚠️  Расхождение: JSON={json_count} vs Файлы={files_count}\n"
            
                stats_text += "\n"
        
            # Сравниваем общее количество - ИСПРАВЛЕННЫЙ ПОДСЧЕТ
            total_json_count = sum(len(chapter['memoir_ids']) for chapter in user_data['chapters'].values())
            sync_status = "🟢" if total_files_count == total_json_count else "🟡"
        
            stats_text += f"{sync_status} **ОБЩАЯ СТАТИСТИКА:**\n"
            stats_text += f"   📚 Глав: {len(user_data['chapters'])}\n"
            stats_text += f"   📝 Всего файлов: {total_files_count}\n"
            stats_text += f"   📋 В JSON: {total_json_count}\n"
        
            if total_files_count == 0 and total_json_count == 0:
                stats_text += "\n💡 **Совет:** Чтобы добавить воспоминания, просто отправьте текстовое или голосовое сообщение!"
            elif total_files_count != total_json_count:
                stats_text += f"\n⚠️  **ВНИМАНИЕ**: Расхождение между файлами и данными JSON!\n"
        
            return stats_text
        
        except Exception as e:
            logger.error(f"Error getting chapters stats: {e}")
            return f"❌ Ошибка при получении статистики: {str(e)}"
    
    def create_chapter(self, user_id: int, chapter_name: str) -> bool:
        """Создать новую главу - с проверкой длины"""
        try:
            # Проверяем длину имени главы
            if len(chapter_name) > 50:
                return False
            
            user_data = self.get_user_data(user_id)
        
            if chapter_name in user_data['chapters']:
                return False
        
            # Создаем папку главы
            chapter_dir = self.get_chapter_dir(user_id, chapter_name)
            logger.info(f"Creating chapter folder: {chapter_dir}")
        
            # Получаем имя папки для сохранения
            folder_name = chapter_dir.name
        
            # Сохраняем данные пользователя
            user_data['chapters'][chapter_name] = {
                'title': chapter_name,
                'memoir_ids': [],
                'created_at': datetime.now().isoformat(),
                'folder_name': folder_name
            }
        
            user_data['current_chapter'] = chapter_name
            self.save_user_data(user_id, user_data)
        
            return chapter_dir.exists()
            
        except Exception as e:
            logger.error(f"Error creating chapter: {e}")
            return False
    
    def delete_chapter(self, user_id: int, chapter_name: str) -> bool:
        """Удалить главу и всю её папку - исправленная логика"""
        try:
            user_data = self.get_user_data(user_id)
            
            if chapter_name not in user_data['chapters']:
                return False
            
            # Получаем имя папки главы
            folder_name = user_data['chapters'][chapter_name].get('folder_name')
            if folder_name:
                chapter_dir = self.get_user_dir(user_id) / "chapters" / folder_name
                if chapter_dir.exists():
                    # Удаляем всю папку главы
                    try:
                        shutil.rmtree(chapter_dir)
                        logger.info(f"Deleted chapter folder: {chapter_dir}")
                    except OSError as e:
                        logger.error(f"Error deleting chapter folder: {e}")
            
            # Удаляем главу из данных пользователя
            del user_data['chapters'][chapter_name]
            
            # Если удаляемая глава была текущей, выбираем другую
            if user_data['current_chapter'] == chapter_name:
                if user_data['chapters']:
                    # Выбираем первую доступную главу
                    user_data['current_chapter'] = next(iter(user_data['chapters'].keys()))
                else:
                    # Создаем главу general, если не осталось глав
                    user_data['chapters']['general'] = {
                        'title': 'general',
                        'memoir_ids': [],
                        'created_at': datetime.now().isoformat(),
                        'folder_name': 'general'
                    }
                    user_data['current_chapter'] = "general"
                    # Создаем папку для general
                    self.get_chapter_dir(user_id, "general")
            
            self.save_user_data(user_id, user_data)
            return True
            
        except Exception as e:
            logger.error(f"Error deleting chapter: {e}")
            return False
    
    def get_user_chapters_list(self, user_id: int) -> list:
        """Получить список глав пользователя"""
        try:
            user_data = self.get_user_data(user_id)
            return list(user_data['chapters'].keys())
        except Exception as e:
            logger.error(f"Error getting chapters list: {e}")
            return []
    
    def set_current_chapter(self, user_id: int, chapter_name: str) -> bool:
        """Установить текущую главу - исправленная логика"""
        try:
            user_data = self.get_user_data(user_id)
            
            if chapter_name not in user_data['chapters']:
                return False
            
            user_data['current_chapter'] = chapter_name
            # ВАЖНО: Сохраняем данные пользователя
            self.save_user_data(user_id, user_data)
            return True
        except Exception as e:
            logger.error(f"Error setting current chapter: {e}")
            return False
    
    def check_ai_processing_result(self, processing_key: str) -> bool:
        """Проверить наличие результата обработки ИИ"""
        try:
            result_file = self.ai_processing_dir / f"{processing_key}.txt"
            return result_file.exists()
        except Exception:
            return False
    
    def get_ai_processing_result(self, processing_key: str) -> str:
        """Получить результат обработки ИИ"""
        try:
            result_file = self.ai_processing_dir / f"{processing_key}.txt"
            if result_file.exists():
                with open(result_file, 'r', encoding='utf-8') as f:
                    result = f.read()
                # Удаляем файл после чтения
                result_file.unlink(missing_ok=True)
                return result
            return ""
        except Exception:
            return ""
    
    def add_donation(self, user_id: int, amount: float):
        """Добавить запись о донате"""
        try:
            user_data = self.get_user_data(user_id)
            user_data['donations_total'] = user_data.get('donations_total', 0) + amount
            user_data['last_donation_date'] = datetime.now().isoformat()
            self.save_user_data(user_id, user_data)
        except Exception as e:
            logger.error(f"Error adding donation: {e}")
    
    def get_chapter_memoirs_list(self, user_id: int, chapter: str) -> List[Dict[str, Any]]:
        """Получить список всех воспоминаний главы с данными"""
        try:
            user_data = self.get_user_data(user_id)
            
            if chapter not in user_data['chapters']:
                return []
            
            folder_name = user_data['chapters'][chapter].get('folder_name', chapter)
            chapter_dir = self.get_user_dir(user_id) / "chapters" / folder_name
            
            if not chapter_dir.exists():
                return []
            
            memoirs = []
            for file_path in chapter_dir.glob("*.txt"):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        memoir_data = json.load(f)
                        memoirs.append(memoir_data)
                except (json.JSONDecodeError, IOError) as e:
                    logger.error(f"Error reading memoir file {file_path}: {e}")
                    continue
            
            # Сортируем по времени создания
            memoirs.sort(key=lambda x: x.get('created_at', ''))
            return memoirs
                
        except Exception as e:
            logger.error(f"Error getting chapter memoirs list: {e}")
            return []
    
    def get_memoir_by_index(self, user_id: int, chapter: str, index: int) -> Optional[Dict[str, Any]]:
        """Получить запись по индексу в главе"""
        memoirs = self.get_chapter_memoirs_list(user_id, chapter)
        if 0 <= index < len(memoirs):
            return memoirs[index]
        return None
    
    def delete_memoir_by_id(self, user_id: int, chapter: str, memoir_id: str) -> bool:
        """Удалить запись по ID"""
        try:
            user_data = self.get_user_data(user_id)
            
            if chapter not in user_data['chapters']:
                return False
            
            # Получаем папку главы
            folder_name = user_data['chapters'][chapter].get('folder_name', chapter)
            chapter_dir = self.get_user_dir(user_id) / "chapters" / folder_name
            
            # Удаляем файл записи
            memoir_file = chapter_dir / f"{memoir_id}.txt"
            if memoir_file.exists():
                memoir_file.unlink()
            
            # Удаляем из данных пользователя
            if memoir_id in user_data['chapters'][chapter]['memoir_ids']:
                user_data['chapters'][chapter]['memoir_ids'].remove(memoir_id)
                user_data['memoirs_count'] = max(0, user_data.get('memoirs_count', 1) - 1)
                self.save_user_data(user_id, user_data)
            
            return True
            
        except Exception as e:
            logger.error(f"Error deleting memoir {memoir_id}: {e}")
            return False

# Инициализация менеджера воспоминаний
memoir_manager = MemoirManager(BASE_DIR)

# Функции для работы с донатами
def generate_yoomoney_url(amount: float, description: str = "Поддержка бота-мемуариста") -> str:
    """Генерация URL для оплаты через ЮMoney"""
    wallet = YOOMONEY_WALLET or ""
    base_url = f"https://yoomoney.ru/to/{wallet}"
    
    return f"{base_url}"

def get_donation_keyboard():
    """Клавиатура для донатов"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💰 Donate", callback_data="donate_50"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")
        ]
    ])

def get_main_keyboard():
    """Основная клавиатура"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📖 Создать главу", callback_data="create_chapter")
        ],
        [
            InlineKeyboardButton(text="📚 Выбрать главу", callback_data="select_chapter")
        ],
        [
            InlineKeyboardButton(text="👁️ Просмотреть главу", callback_data="view_chapter")
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="stats")
        ],
        [
            InlineKeyboardButton(text="🔄 Обработать ИИ", callback_data="enhance_chapter")
        ],
        [
            InlineKeyboardButton(text="🗑️ Удалить главу", callback_data="delete_chapter")
        ],
        [
            InlineKeyboardButton(text="🔍 Навигация по записям", callback_data="navigate_memoirs")
        ],
        [
            InlineKeyboardButton(text="💝 Поддержать проект", callback_data="donate_menu")
        ]
    ])

def get_chapters_keyboard(user_id: int):
    """Клавиатура для выбора главы - с ограничением длины callback_data"""
    try:
        chapters = memoir_manager.get_user_chapters_list(user_id)
        keyboard = []
        
        # Добавляем кнопки глав (по 2 в ряд)
        for i in range(0, len(chapters), 2):
            row = []
            for chapter in chapters[i:i+2]:
                # Ограничиваем длину callback_data до 64 байтов
                # Используем только первые 30 символов для callback_data
                safe_chapter = chapter[:30]
                row.append(InlineKeyboardButton(text=chapter, callback_data=f"chapter_{safe_chapter}"))
            keyboard.append(row)
        
        # Добавляем кнопку назад
        keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    except Exception as e:
        logger.error(f"Error creating chapters keyboard: {e}")
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")]])

def get_chapter_view_keyboard():
    """Клавиатура для просмотра главы - обновленная с навигацией"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📝 Исходный текст", callback_data="view_original"),
            InlineKeyboardButton(text="✨ ИИ-обработанный", callback_data="view_enhanced")
        ],
        [
            InlineKeyboardButton(text="🧙 Пелевин", callback_data="view_pelevin"),
            InlineKeyboardButton(text="📚 Толстой", callback_data="view_tolstoy")
        ],
        [
            InlineKeyboardButton(text="🌀 Довлатов", callback_data="view_dovlatov"),
            InlineKeyboardButton(text="🍷 Буковски", callback_data="view_bukowski")
        ],
        [
            InlineKeyboardButton(text="👻 Кинг", callback_data="view_king"),
            InlineKeyboardButton(text="⚡ Роулинг", callback_data="view_rowling")
        ],
        [
            InlineKeyboardButton(text="🔍 Оруэлл", callback_data="view_orwell"),
            InlineKeyboardButton(text="🎭 Достоевский", callback_data="view_dostoevsky")
        ],
        [
            InlineKeyboardButton(text="📖 Чехов", callback_data="view_chekhov"),
            InlineKeyboardButton(text="👻 Гоголь", callback_data="view_gogol")
        ],
        [
            InlineKeyboardButton(text="✍️ Пушкин", callback_data="view_pushkin"),
            InlineKeyboardButton(text="🎩 Набоков", callback_data="view_nabokov")
        ],
        [
            InlineKeyboardButton(text="🎣 Хемингуэй", callback_data="view_hemingway"),
            InlineKeyboardButton(text="🌀 Кафка", callback_data="view_kafka")
        ],
        [
            InlineKeyboardButton(text="📣 Маяковский", callback_data="view_mayakovsky"),
            InlineKeyboardButton(text="🎭 Шекспир", callback_data="view_shakespeare")
        ],
        [
            InlineKeyboardButton(text="🩸 Сорокин", callback_data="view_sorokin"),
            InlineKeyboardButton(text="🌾 Есенин", callback_data="view_yesenin")
        ],
        [
            InlineKeyboardButton(text="🎨 Все стили", callback_data="view_all_styles")
        ],
        [
            InlineKeyboardButton(text="🔍 Навигация по записям", callback_data="navigate_memoirs")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_main")
        ]
    ])

def get_style_selection_keyboard():
    """Клавиатура для выбора стиля при просмотре всех стилей"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🧙 Пелевин", callback_data="view_pelevin"),
            InlineKeyboardButton(text="📚 Толстой", callback_data="view_tolstoy")
        ],
        [
            InlineKeyboardButton(text="🌀 Довлатов", callback_data="view_dovlatov"),
            InlineKeyboardButton(text="🍷 Буковски", callback_data="view_bukowski")
        ],
        [
            InlineKeyboardButton(text="👻 Кинг", callback_data="view_king"),
            InlineKeyboardButton(text="⚡ Роулинг", callback_data="view_rowling")
        ],
        [
            InlineKeyboardButton(text="🔍 Оруэлл", callback_data="view_orwell"),
            InlineKeyboardButton(text="🎭 Достоевский", callback_data="view_dostoevsky")
        ],
        [
            InlineKeyboardButton(text="📖 Чехов", callback_data="view_chekhov"),
            InlineKeyboardButton(text="👻 Гоголь", callback_data="view_gogol")
        ],
        [
            InlineKeyboardButton(text="✍️ Пушкин", callback_data="view_pushkin"),
            InlineKeyboardButton(text="🎩 Набоков", callback_data="view_nabokov")
        ],
        [
            InlineKeyboardButton(text="🎣 Хемингуэй", callback_data="view_hemingway"),
            InlineKeyboardButton(text="🌀 Кафка", callback_data="view_kafka")
        ],
        [
            InlineKeyboardButton(text="📣 Маяковский", callback_data="view_mayakovsky"),
            InlineKeyboardButton(text="🎭 Шекспир", callback_data="view_shakespeare")
        ],
        [
            InlineKeyboardButton(text="🩸 Сорокин", callback_data="view_sorokin"),
            InlineKeyboardButton(text="🌾 Есенин", callback_data="view_yesenin")
        ],
        [
            InlineKeyboardButton(text="🔙 Назад к просмотру", callback_data="view_chapter")
        ]
    ])

def get_delete_confirmation_keyboard():
    """Клавиатура подтверждения удаления"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete")
        ]
    ])

def get_memoir_navigation_keyboard(current_index: int, total_memoirs: int, memoir_id: str):
    """Клавиатура для навигации по записям главы"""
    keyboard = []
    
    # Кнопки навигации
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"nav_prev_{current_index}"))
    
    nav_buttons.append(InlineKeyboardButton(text=f"{current_index + 1}/{total_memoirs}", callback_data="current_position"))
    
    if current_index < total_memoirs - 1:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"nav_next_{current_index}"))
    
    if nav_buttons:
        keyboard.append(nav_buttons)
    
    # Кнопка удаления текущей записи
    keyboard.append([InlineKeyboardButton(text="🗑️ Удалить эту запись", callback_data=f"delete_memoir_{memoir_id}")])
    
    # Кнопка возврата
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к главе", callback_data="back_to_chapter_view")])
    
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def safe_answer_callback(callback: types.CallbackQuery, text: str = None):
    """Безопасный ответ на callback для избежания ошибок устаревания"""
    try:
        await callback.answer(text)
    except Exception as e:
        logger.warning(f"Callback answer failed (likely timeout): {e}")

async def safe_edit_message(message: types.Message, text: str, **kwargs):
    """Безопасное редактирование сообщения"""
    try:
        await message.edit_text(text, **kwargs)
    except Exception as e:
        logger.warning(f"Message edit failed: {e}")
        # Пытаемся отправить новое сообщение
        try:
            await message.answer(text, **kwargs)
        except Exception as e2:
            logger.error(f"Failed to send new message: {e2}")

async def safe_send_message(chat_id: int, text: str, **kwargs):
    """Безопасная отправка сообщения"""
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except Exception as e:
        logger.error(f"Failed to send message: {e}")

async def split_long_message(text: str, max_length: int = 4000) -> list:
    """Разбить длинное сообщение на части - улучшенный алгоритм"""
    if len(text) <= max_length:
        return [text]
    
    parts = []
    current_part = ""
    
    # Разбиваем по абзацам для сохранения форматирования
    paragraphs = text.split('\n\n')
    
    for paragraph in paragraphs:
        # Если абзац слишком длинный, разбиваем на строки
        if len(paragraph) > max_length:
            lines = paragraph.split('\n')
            for line in lines:
                if len(current_part) + len(line) + 1 > max_length:
                    if current_part:
                        parts.append(current_part.strip())
                        current_part = line + '\n'
                    else:
                        # Если одна строка слишком длинная, разбиваем по словам
                        words = line.split()
                        for word in words:
                            if len(current_part) + len(word) + 1 > max_length:
                                if current_part:
                                    parts.append(current_part.strip())
                                    current_part = word + ' '
                                else:
                                    # Если одно слово слишком длинное, разбиваем по символам
                                    parts.append(word[:max_length-3] + '...')
                                    current_part = word[max_length-3:] + ' '
                            else:
                                current_part += word + ' '
                        current_part += '\n'
                else:
                    current_part += line + '\n'
        else:
            if len(current_part) + len(paragraph) + 2 > max_length:
                if current_part:
                    parts.append(current_part.strip())
                current_part = paragraph + '\n\n'
            else:
                current_part += paragraph + '\n\n'
    
    if current_part.strip():
        parts.append(current_part.strip())
    
    return parts

async def show_memoir_by_index(message: types.Message, user_id: int, chapter: str, index: int, memoirs: list = None):
    """Показать запись по индексу"""
    try:
        if memoirs is None:
            memoirs = memoir_manager.get_chapter_memoirs_list(user_id, chapter)
        
        if not memoirs or index < 0 or index >= len(memoirs):
            await message.answer("❌ Запись не найдена.")
            return
        
        memoir = memoirs[index]
        total_memoirs = len(memoirs)
        
        # Форматируем текст записи
        created_at = datetime.fromisoformat(memoir['created_at']).strftime('%d.%m.%Y %H:%M')
        memoir_text = f"""
📖 Запись {index + 1} из {total_memoirs}

ID: {memoir['id']}
Глава: {chapter}
Создано: {created_at}
Источник: {memoir.get('source', 'text')}

{memoir['text']}
        """
        
        # Создаем клавиатуру навигации
        keyboard = get_memoir_navigation_keyboard(index, total_memoirs, memoir['id'])
        
        await message.answer(memoir_text, reply_markup=keyboard)
        
    except Exception as e:
        logger.error(f"Error showing memoir by index: {e}")
        await message.answer("❌ Ошибка при отображении записи")

async def process_enhanced_view_background(user_id: int, chapter_name: str, text: str, processing_msg: types.Message, style: str = None):
    """Фоновая задача для обработки просмотра с ИИ - исправленная версия"""
    try:
        # Обрабатываем всю главу с помощью ИИ в отдельном потоке
        loop = asyncio.get_event_loop()
        enhanced_text = await loop.run_in_executor(
            None, 
            lambda: memoir_manager.enhance_text_with_ai(text, style=style)
        )
        
        await processing_msg.delete()
        
        # Формируем ответ в зависимости от стиля
        style_names = {
            "pelevin": "🧙 Пелевина",
            "tolstoy": "📚 Толстого", 
            "dovlatov": "🌀 Довлатова",
            "bukowski": "🍷 Буковски", 
            "king": "👻 Кинга",
            "rowling": "⚡ Роулинг",
            "orwell": "🔍 Оруэлла",
            "dostoevsky": "🎭 Достоевского",
            "chekhov": "📖 Чехова",
            "gogol": "👻 Гоголя", 
            "pushkin": "✍️ Пушкина",
            "nabokov": "🎩 Набокова",
            "hemingway": "🎣 Хемингуэя",
            "kafka": "🌀 Кафки",
            "mayakovsky": "📣 Маяковского",
            "shakespeare": "🎭 Шекспира",
            "sorokin": "🩸 Сорокина",
            "yesenin": "🌾 Есенина",
            None: "✨ исправленная"
        }
        
        style_display = style_names.get(style, "✨ исправленная")
        
        # ПЕРВОЕ СООБЩЕНИЕ - заголовок
        header = f"Глава '{chapter_name}' в стиле {style_display}:"
        await safe_send_message(processing_msg.chat.id, header)
        
        # ВТОРОЕ СООБЩЕНИЕ - текст
        # Разбиваем на части если нужно
        parts = await split_long_message(enhanced_text)
        
        for i, part in enumerate(parts, 1):
            if i == 1:
                await safe_send_message(processing_msg.chat.id, part)
            else:
                await safe_send_message(processing_msg.chat.id, part)
                
    except Exception as e:
        logger.error(f"Error in enhanced view background: {e}")
        await safe_edit_message(processing_msg, f"❌ Ошибка при обработке ИИ: {str(e)}")

async def process_chapter_with_ai_background(user_id: int, chapter_name: str, text: str, processing_key: str, processing_msg: types.Message):
    """Фоновая задача для обработки главы с помощью ИИ - исправленная версия"""
    try:
        # Запускаем обработку ИИ в отдельном потоке
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None, 
            lambda: memoir_manager.enhance_text_with_ai(text, processing_key)
        )
        
        # Ждем появления результата
        max_attempts = 30  # Максимум 30 попыток (60 секунд)
        for attempt in range(max_attempts):
            if memoir_manager.check_ai_processing_result(processing_key):
                break
            await asyncio.sleep(2)  # Ждем 2 секунды между проверками
        
        # Получаем результат
        result = memoir_manager.get_ai_processing_result(processing_key)

        if not result:
            result = "❌ Результат обработки не получен"
        
        # УДАЛЯЕМ сообщение о обработке
        await processing_msg.delete()
        
        # ПЕРВОЕ СООБЩЕНИЕ - заголовок
        if "❌ Ошибка" in result:
            header = f"❌ Ошибка при обработке главы '{chapter_name}'"
        else:
            header = f"✨ Обработанная глава: {chapter_name}"
        
        await safe_send_message(processing_msg.chat.id, header)
        
        # ВТОРОЕ СООБЩЕНИЕ - текст с разметкой
        if "❌ Ошибка" not in result:
            # Разбиваем на части если нужно
            parts = await split_long_message(result)
            
            for i, part in enumerate(parts, 1):
                if i == 1:
                    # Первую часть отправляем как есть
                    await safe_send_message(processing_msg.chat.id, part)
                else:
                    # Остальные части отправляем как продолжение
                    await safe_send_message(processing_msg.chat.id, part)
            
    except Exception as e:
        logger.error(f"Background AI processing error: {e}")
        await safe_edit_message(processing_msg, f"❌ Произошла ошибка при обработке ИИ: {str(e)}")

# Обработчики команд
@router.message(Command(commands=["start"]))
async def cmd_start(message: types.Message, state: FSMContext):
    """Обработчик команды /start"""
    await state.clear()
    
    try:
        user_data = memoir_manager.get_user_data(message.from_user.id)
        if not user_data.get('username'):
            user_data['username'] = message.from_user.username or message.from_user.first_name
            memoir_manager.save_user_data(message.from_user.id, user_data)
        
        # Создаем папку для главы general
        memoir_manager.get_chapter_dir(message.from_user.id, "general")
        
        welcome_text = f"""
👋 Привет, {message.from_user.first_name}!

Я бот-мемуарист. Записывай воспоминания текстом или голосовыми сообщениями!

💡 Просто напиши или запиши голосовое сообщение - я всё сохраню.

✨ Новые возможности: 18 стилей известных писателей для обработки текста!

Текущая глава: {user_data['current_chapter']}
        """
        
        await message.answer(welcome_text, reply_markup=get_main_keyboard())
        await state.set_state(MemoirStates.waiting_for_memoir)
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        await message.answer("❌ Произошла ошибка при запуске бота.")

@router.message(Command(commands=["help"]))
async def cmd_help(message: types.Message):
    """Обработчик команды /help"""
    help_text = """
# 📖 Краткое руководство пользователя для бота-мемуариста

## 🚀 Начало работы
1. **Запуск**: Отправьте команду `/start`
2. **Первая запись**: Просто напишите текстовое сообщение или отправьте голосовое
3. **Главы**: Все записи автоматически сохраняются в текущую главу

## 📋 Основные возможности

### 💾 Сохранение воспоминаний
- **Текст**: Просто напишите сообщение (минимум 5 символов)
- **Голосовые**: Отправьте голосовое сообщение → бот преобразует в текст
- **Автосохранение**: Все записи сразу сохраняются в текущую главу

### 📚 Управление главами
- **Создать главу**: "📖 Создать главу" → введите название
- **Выбрать главу**: "📚 Выбрать главу" → выберите из списка
- **Удалить главу**: "🗑️ Удалить главу" (нельзя удалить последнюю)

### 👁️ Просмотр записей
- **Весь текст главы**: "👁️ Просмотреть главу" → "📝 Исходный текст"
- **По одной записи**: "🔍 Навигация по записям" 
  - Листайте стрелками ⬅️➡️
  - Удаляйте отдельные записи 🗑️

### ✨ ИИ-обработка
- **Коррекция**: "✨ ИИ-обработанный" - исправление ошибок
- **Стили писателей**:
  - 🧙 Пелевин - мистика, философия
  - 📚 Толстой - психологизм, мораль
  - 🌀 Довлатов - юмор, легкость
  - 🍷 Буковски - грубый реализм
  - 👻 Кинг - хоррор, напряжение
  - ⚡ Роулинг - фэнтези, магия
  - 🔍 Оруэлл - антиутопия, критика
  - 🎭 Достоевский - психологизм, трагедия
  - 📖 Чехов - лаконичность, подтекст
  - 👻 Гоголь - гротеск, сатира
  - ✍️ Пушкин - ясность, гармония
  - 🎩 Набоков - игра слов, эстетика
  - 🎣 Хемингуэй - минимализм, подтекст
  - 🌀 Кафка - абсурд, тревога
  - 📣 Маяковский - плакатность, «лесенка»
  - 🎭 Шекспир - высокий слог, трагедия
  - 🩸 Сорокин - постмодерн, абсурд
  - 🌾 Есенин - лирика, образы деревни

### 📊 Статистика
- "📊 Статистика" - обзор всех глав и записей
- Видна текущая глава ⭐
- Сравнение файлов и данных JSON

## 🛠️ Быстрые команды
- `/start` - перезапуск бота
- `/help` - полная справка
- `/stats` - статистика
- `/donate` - поддержать проект

## 💡 Советы
1. **Создавайте тематические главы** для организации воспоминаний
2. **Используйте навигацию** для просмотра длинных глав
3. **Экспериментируйте со стилями** для творческой обработки
4. **Проверяйте статистику** для контроля за записями

## 💝 Поддержка проекта
- Бот бесплатный, но вы можете поддержать развитие
- "💝 Поддержать проект" → выбор суммы доната

    """
    await message.answer(help_text, reply_markup=get_main_keyboard())

@router.message(Command(commands=["stats"]))
async def cmd_stats(message: types.Message):
    """Статистика воспоминаний - обновленная версия"""
    try:
        stats_text = memoir_manager.get_user_chapters_stats(message.from_user.id)
        await message.answer(stats_text, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error in stats command: {e}")
        await message.answer("❌ Ошибка при получении статистики")

@router.message(Command(commands=["donate"]))
async def cmd_donate(message: types.Message):
    """Команда для донатов"""
    await show_donation_menu(message)

async def show_donation_menu(message: types.Message):
    """Показать меню донатов"""
    donation_text = """
💝 Поддержать проект

Бот работает бесплатно, но его развитие требует ресурсов:
• 💰 Оплата серверов
• 🔧 Техническая поддержка  
• 🚀 Разработка новых функций

Ваша поддержка помогает улучшать бота!
    """
    
    await message.answer(donation_text, reply_markup=get_donation_keyboard())

# Обработчики донатов
@router.callback_query(F.data == "donate_menu")
async def callback_donate_menu(callback: types.CallbackQuery):
    """Меню донатов"""
    await safe_answer_callback(callback)
    await show_donation_menu(callback.message)

@router.callback_query(F.data.startswith("donate_"))
async def callback_donate(callback: types.CallbackQuery, state: FSMContext):
    """Обработка донатов"""
    await safe_answer_callback(callback)
    
    data = callback.data
    user_id = callback.from_user.id
    
    if data == "donate_info":
        info_text = """
❤️ О проекте

Этот бот создан для сохранения ваших воспоминаний и мыслей. 
Все средства от донатов идут на:

• 📊 Аренду серверов для хранения ваших данных
• 🔐 Обеспечение безопасности и конфиденциальности  
• 🛠 Техническое обслуживание и обновления
• 🚀 Разработку новых функций

Спасибо за вашу поддержку! 🙏
        """
        await callback.message.answer(info_text, reply_markup=get_donation_keyboard())
        return
    
    else:
        # Обработка фиксированных сумм
        amounts = {
            "donate_50": 50,
            "donate_100": 100, 
            "donate_200": 200,
            "donate_500": 500
        }
        
        amount = amounts.get(data, 100)
        await process_donation_payment(callback.message, user_id, amount)

async def process_donation_payment(message: types.Message, user_id: int, amount: float):
    """Обработка платежа доната"""
    payment_url = generate_yoomoney_url(amount, f"Донат от пользователя {user_id}")
    
    payment_text = f"""
💝 Донат

Для завершения платежа:

1. Нажмите на кнопку ниже для перехода к оплате
2. Следуйте инструкциям в ЮMoney
3. После оплаты вы вернетесь в бот

Спасибо за вашу поддержку! 🙏
    """
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)],
        [InlineKeyboardButton(text="🔙 Назад к донатам", callback_data="donate_menu")]
    ])
    
    await message.answer(payment_text, reply_markup=keyboard)
    
    # Логируем донат
    memoir_manager.add_donation(user_id, amount)
    logger.info(f"User {user_id} initiated donation: {amount} rub")

@router.message(MemoirStates.waiting_donation_amount)
async def process_custom_donation(message: types.Message, state: FSMContext):
    """Обработка произвольной суммы доната"""
    try:
        amount_text = message.text.strip()
        amount = float(amount_text)
        
        if amount < 10:
            await message.answer("❌ Минимальная сумма доната - 10 рублей")
            return
        elif amount > 50000:
            await message.answer("❌ Максимальная сумма доната - 50,000 рублей")
            return
        
        await process_donation_payment(message, message.from_user.id, amount)
        await state.set_state(MemoirStates.waiting_for_memoir)
        
    except ValueError:
        await message.answer("❌ Пожалуйста, введите корректную сумму (например: 150)")

# Обработчик состояния создания главы
@router.message(MemoirStates.creating_chapter)
async def process_chapter_name(message: types.Message, state: FSMContext):
    """Обработчик ввода названия новой главы"""
    if not message.text:
        await message.answer("❌ Название главы не может быть пустым. Попробуйте снова:")
        return
        
    chapter_name = message.text.strip()
    
    if not chapter_name:
        await message.answer("❌ Название главы не может быть пустым. Попробуйте снова:")
        return
    
    if len(chapter_name) > 50:
        await message.answer("❌ Название главы слишком длинное. Максимум 50 символов.")
        return
    
    success = memoir_manager.create_chapter(message.from_user.id, chapter_name)
    
    if success:
        await message.answer(f"✅ Глава '{chapter_name}' создана и выбрана текущей!", reply_markup=get_main_keyboard())
    else:
        await message.answer(f"❌ Глава '{chapter_name}' уже существует! Попробуйте другое название:")
        return
    
    await state.set_state(MemoirStates.waiting_for_memoir)

# Обработчик текстовых сообщений
@router.message(StateFilter(MemoirStates.waiting_for_memoir), F.text & ~F.text.startswith('/'))
async def handle_memoir_text(message: types.Message, state: FSMContext):
    """Обработчик текстовых воспоминаний"""
    user_id = message.from_user.id
    text = message.text.strip()
    
    if not text or len(text) < 5:
        await message.answer("❌ Текст слишком короткий. Напишите хотя бы 5 символов.")
        return
    
    try:
        # Сохраняем сырой текст
        memoir_id = memoir_manager.save_raw_memoir(user_id, text, "text")
        
        if memoir_id.startswith("error_"):
            await message.answer("❌ Произошла ошибка при сохранении текста.")
            return
        
        # Добавляем в текущую главу (автоматически через папку)
        user_data = memoir_manager.get_user_data(user_id)
        
        # Отправляем подтверждение
        response = f"""
✅ Воспоминание сохранено!

ID: {memoir_id}
Глава: {user_data['current_chapter']}
Текст: {text[:4000]}...
        """
        
        await message.answer(response)
        
    except Exception as e:
        logger.error(f"Error processing text memoir: {e}")
        await message.answer("❌ Произошла ошибка при сохранении текста.")

# Обработчик голосовых сообщений
@router.message(StateFilter(MemoirStates.waiting_for_memoir), F.voice)
async def handle_voice_message(message: types.Message):
    """Обработчик голосовых сообщений - улучшенная обработка ошибок"""
    user_id = message.from_user.id
    
    try:
        # Скачиваем голосовое сообщение
        voice = message.voice
        file_info = await bot.get_file(voice.file_id)
        file_path = file_info.file_path
        
        # Создаем временную папку
        temp_dir = BASE_DIR / "temp"
        temp_dir.mkdir(exist_ok=True)

        temp_file = temp_dir / f"{voice.file_id}.oga"
        
        # Скачиваем файл
        await bot.download_file(file_path, destination=temp_file)
        
        # Отправляем сообщение о обработке
        processing_msg = await message.answer("🎤 Обрабатываю голосовое сообщение...")
        
        # Транскрибируем голос в текст
        text = await memoir_manager.transcribe_voice(temp_file)
        
        # Удаляем временный файл
        temp_file.unlink(missing_ok=True)
        
        if not text or "Не удалось распознать" in text or "Ошибка" in text:
            await safe_edit_message(processing_msg, f"❌ {text if text else 'Не удалось распознать речь'}")
            return
        
        # Сохраняем распознанный текст
        memoir_id = memoir_manager.save_raw_memoir(user_id, text, "voice")
        
        if memoir_id.startswith("error_"):
            await safe_edit_message(processing_msg, "❌ Произошла ошибка при сохранении голосового сообщения.")
            return
        
        # Добавляем в текущую главу (автоматически через папку)
        user_data = memoir_manager.get_user_data(user_id)
        
        # Обновляем сообщение о обработке
        response = f"""
✅ Голосовое сообщение сохранено!

ID: {memoir_id}
Глава: {user_data['current_chapter']}
Распознанный текст: {text[:4000]}...
        """
        
        await safe_edit_message(processing_msg, response)
        
    except Exception as e:
        logger.error(f"Error processing voice message: {e}")
        await message.answer("❌ Произошла ошибка при обработке голосового сообщения.")

# Обработчики callback-запросов
@router.callback_query(F.data == "help")
async def callback_help(callback: types.CallbackQuery):
    await safe_answer_callback(callback)
    await cmd_help(callback.message)

@router.callback_query(F.data == "stats")
async def callback_stats(callback: types.CallbackQuery):
    await safe_answer_callback(callback)
    try:
        # ИСПРАВЛЕНО: используем callback.from_user.id вместо callback.message
        user_id = callback.from_user.id
        logger.info(f"callback_stats: User ID = {user_id}")
        stats_text = memoir_manager.get_user_chapters_stats(user_id)
        await callback.message.answer(stats_text, reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error in stats callback: {e}")
        await callback.message.answer("❌ Ошибка при получении статистики")

@router.callback_query(F.data == "create_chapter")
async def callback_create_chapter(callback: types.CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    await callback.message.answer("📖 Введите название новой главы:")
    await state.set_state(MemoirStates.creating_chapter)

@router.callback_query(F.data == "select_chapter")
async def callback_select_chapter(callback: types.CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    
    try:
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        
        if not user_data['chapters'] or len(user_data['chapters']) == 0:
            await callback.message.answer("📚 У вас пока нет глав. Создайте первую!")
            return
        
        keyboard = get_chapters_keyboard(callback.from_user.id)
        await callback.message.answer(
            "📚 Выберите главу для работы:",
            reply_markup=keyboard
        )
        await state.set_state(MemoirStates.selecting_chapter)
    except Exception as e:
        logger.error(f"Error in select_chapter: {e}")
        await callback.message.answer("❌ Ошибка при выборе главы")

@router.callback_query(F.data.startswith("chapter_"), StateFilter(MemoirStates.selecting_chapter))
async def callback_chapter_selected(callback: types.CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    
    try:
        chapter_name = callback.data.replace("chapter_", "")
        
        success = memoir_manager.set_current_chapter(callback.from_user.id, chapter_name)
        
        if success:
            await callback.message.answer(f"✅ Текущая глава изменена на: {chapter_name}")
        else:
            await callback.message.answer("❌ Ошибка при выборе главы.")
        
        await state.set_state(MemoirStates.waiting_for_memoir)
        await callback.message.answer("🔙 Главное меню:", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error in chapter_selected: {e}")
        await callback.message.answer("❌ Ошибка при выборе главы")

@router.callback_query(F.data == "view_chapter")
async def callback_view_chapter(callback: types.CallbackQuery):
    await safe_answer_callback(callback)
    
    try:
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        
        if not user_data['chapters'] or len(user_data['chapters']) == 0:
            await callback.message.answer("📚 У вас пока нет глав. Создайте первую!")
            return
        
        await callback.message.answer(
            f"📖 Выберите вариант просмотра для главы '{user_data['current_chapter']}':",
            reply_markup=get_chapter_view_keyboard()
        )
    except Exception as e:
        logger.error(f"Error in view_chapter: {e}")
        await callback.message.answer("❌ Ошибка при просмотре главы")

@router.callback_query(F.data == "view_all_styles")
async def callback_view_all_styles(callback: types.CallbackQuery):
    """Показать все доступные стили"""
    await safe_answer_callback(callback)
    await callback.message.answer(
        "🎨 Выберите стиль писателя для обработки текста:",
        reply_markup=get_style_selection_keyboard()
    )

@router.callback_query(F.data == "view_original")
async def callback_view_original(callback: types.CallbackQuery):
    """Просмотр исходного текста главы"""
    await safe_answer_callback(callback, "🔄 Загружаю главу...")
    
    try:
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        current_chapter = user_data['current_chapter']
        
        # Получаем текст главы
        chapter_text = memoir_manager.get_chapter_memoirs(callback.from_user.id, current_chapter, enhanced=False)
        
        # Разбиваем на части если нужно
        parts = await split_long_message(chapter_text)
        
        for i, part in enumerate(parts, 1):
            if i == 1:
                await callback.message.answer(f"Глава: {current_chapter} (Часть {i}/{len(parts)})\n{part}")
            else:
                await safe_send_message(callback.message.chat.id, f"Часть {i}/{len(parts)}\n{part}")
        
    except Exception as e:
        logger.error(f"Error in view_original: {e}")
        await safe_send_message(callback.message.chat.id, "❌ Ошибка при просмотре главы")

@router.callback_query(F.data == "view_enhanced")
async def callback_view_enhanced(callback: types.CallbackQuery):
    """Просмотр ИИ-обработанного текста главы - исправленная логика"""
    await safe_answer_callback(callback, "🔄 Начинаю обработку...")
    
    try:
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        current_chapter = user_data['current_chapter']
        
        if not DEEPSEEK_API_KEY:
            await callback.message.answer("⚠️ Функция ИИ-обработки недоступна. Настройте API ключ DeepSeek.")
            return
        
        processing_msg = await callback.message.answer("🔄 Исправляю орфографию и пунктуацию...")
        
        # Получаем исходные воспоминания как единый текст
        original_text = memoir_manager.get_chapter_memoirs(callback.from_user.id, current_chapter, enhanced=True)
        
        if "нет воспоминаний" in original_text or "Ошибка" in original_text:
            await safe_edit_message(processing_msg, original_text)
            return
        
        # Используем фоновую задачу для обработки ИИ
        asyncio.create_task(process_enhanced_view_background(
            callback.from_user.id, 
            current_chapter, 
            original_text, 
            processing_msg,
            style=None
        ))
        
    except Exception as e:
        logger.error(f"Error in view_enhanced: {e}")
        await safe_send_message(callback.message.chat.id, "❌ Ошибка при обработке ИИ")

# Обработчики для стилей писателей
@router.callback_query(F.data.in_(["view_pelevin", "view_tolstoy", "view_dovlatov", "view_bukowski", "view_king", "view_rowling", "view_orwell", "view_dostoevsky", "view_chekhov", "view_gogol", "view_pushkin", "view_nabokov", "view_hemingway", "view_kafka", "view_mayakovsky", "view_shakespeare", "view_sorokin", "view_yesenin"]))
async def callback_view_style(callback: types.CallbackQuery):
    """Обработчик для всех стилей писателей"""
    style_map = {
        "view_pelevin": "🧙 Пелевин",
        "view_tolstoy": "📚 Толстой", 
        "view_dovlatov": "🌀 Довлатов",
        "view_bukowski": "🍷 Буковски",
        "view_king": "👻 Кинг",
        "view_rowling": "⚡ Роулинг",
        "view_orwell": "🔍 Оруэлл",
        "view_dostoevsky": "🎭 Достоевский",
        "view_chekhov": "📖 Чехов",
        "view_gogol": "👻 Гоголь",
        "view_pushkin": "✍️ Пушкин",
        "view_nabokov": "🎩 Набоков",
        "view_hemingway": "🎣 Хемингуэй",
        "view_kafka": "🌀 Кафка",
        "view_mayakovsky": "📣 Маяковский",
        "view_shakespeare": "🎭 Шекспир",
        "view_sorokin": "🩸 Сорокин",
        "view_yesenin": "🌾 Есенин"
    }
    
    style_name = style_map.get(callback.data, "стиль")
    await safe_answer_callback(callback, f"🔄 Начинаю стилизацию под {style_name}...")
    
    try:
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        current_chapter = user_data['current_chapter']
        
        if not DEEPSEEK_API_KEY:
            await callback.message.answer("⚠️ Функция ИИ-обработки недоступна. Настройте API ключ DeepSeek.")
            return
        
        processing_msg = await callback.message.answer(f"🔄 Стилизую в стиле {style_name}...")
        
        # Получаем исходные воспоминания как единый текст
        original_text = memoir_manager.get_chapter_memoirs(callback.from_user.id, current_chapter, enhanced=True)
        
        if "нет воспоминаний" in original_text or "Ошибка" in original_text:
            await safe_edit_message(processing_msg, original_text)
            return
        
        # Используем фоновую задачу для обработки ИИ с указанным стилем
        asyncio.create_task(process_enhanced_view_background(
            callback.from_user.id, 
            current_chapter, 
            original_text, 
            processing_msg,
            style=callback.data.replace("view_", "")
        ))
        
    except Exception as e:
        logger.error(f"Error in {callback.data}: {e}")
        await safe_send_message(callback.message.chat.id, f"❌ Ошибка при стилизации под {style_name}")

@router.callback_query(F.data == "enhance_chapter")
async def callback_enhance_chapter(callback: types.CallbackQuery):
    """Обработка всей текущей главы с помощью ИИ в фоне"""
    await safe_answer_callback(callback)
    
    try:
        if not DEEPSEEK_API_KEY:
            await callback.message.answer("⚠️ Функция ИИ-обработки недоступна. Настройте API ключ DeepSeek.")
            return
        
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        current_chapter = user_data['current_chapter']
        
        # Получаем все воспоминания главы как единый текст
        chapter_text = memoir_manager.get_chapter_memoirs(callback.from_user.id, current_chapter, enhanced=True)
        
        if "нет воспоминаний" in chapter_text or "Ошибка" in chapter_text:
            await callback.message.answer(chapter_text)
            return
        
        # Генерируем уникальный ключ для обработки
        processing_key = f"{callback.from_user.id}_{uuid.uuid4().hex}"
        
        # Отправляем сообщение о начале обработки
        processing_msg = await callback.message.answer("🔄 Обработка ИИ запущена. Ожидайте...")
        
        # Запускаем фоновую задачу
        asyncio.create_task(process_chapter_with_ai_background(
            callback.from_user.id, 
            current_chapter, 
            chapter_text, 
            processing_key, 
            processing_msg
        ))
        
    except Exception as e:
        logger.error(f"Error in enhance_chapter: {e}")
        await callback.message.answer("❌ Ошибка при запуске обработки ИИ")

@router.callback_query(F.data == "delete_chapter")
async def callback_delete_chapter(callback: types.CallbackQuery, state: FSMContext):
    """Обработчик удаления главы"""
    await safe_answer_callback(callback)
    
    try:
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        current_chapter = user_data['current_chapter']
        
        if not user_data['chapters'] or len(user_data['chapters']) <= 1:
            await callback.message.answer("❌ Нельзя удалить последнюю главу!")
            return
        
        await callback.message.answer(
            f"⚠️ Вы уверены, что хотите удалить главу '{current_chapter}'?\n\n"
            f"Это действие нельзя отменить! Вся папка главы со всеми воспоминаниями будет удалена.",
            reply_markup=get_delete_confirmation_keyboard()
        )
        await state.set_state(MemoirStates.deleting_chapter)
    except Exception as e:
        logger.error(f"Error in delete_chapter: {e}")
        await callback.message.answer("❌ Ошибка при удалении главы")

@router.callback_query(F.data == "confirm_delete", StateFilter(MemoirStates.deleting_chapter))
async def callback_confirm_delete(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение удаления главы"""
    await safe_answer_callback(callback)
    
    try:
        user_data = memoir_manager.get_user_data(callback.from_user.id)
        current_chapter = user_data['current_chapter']
        
        # Удаляем главу и её папку
        success = memoir_manager.delete_chapter(callback.from_user.id, current_chapter)
        
        if success:
            await callback.message.answer(f"✅ Глава '{current_chapter}' и все её воспоминания успешно удалены!")
        else:
            await callback.message.answer("❌ Ошибка при удалении главы.")
        
        await state.set_state(MemoirStates.waiting_for_memoir)
        await callback.message.answer("🔙 Главное меню:", reply_markup=get_main_keyboard())
    except Exception as e:
        logger.error(f"Error in confirm_delete: {e}")
        await callback.message.answer("❌ Ошибка при подтверждении удаления")

@router.callback_query(F.data == "cancel_delete", StateFilter(MemoirStates.deleting_chapter))
async def callback_cancel_delete(callback: types.CallbackQuery, state: FSMContext):
    """Отмена удаления главы"""
    await safe_answer_callback(callback)
    await callback.message.answer("❌ Удаление отменено.")
    await state.set_state(MemoirStates.waiting_for_memoir)
    await callback.message.answer("🔙 Главное меню:", reply_markup=get_main_keyboard())

@router.callback_query(F.data == "navigate_memoirs")
async def callback_navigate_memoirs(callback: types.CallbackQuery, state: FSMContext):
    """Начать навигацию по записям текущей главы"""
    await safe_answer_callback(callback)
    
    try:
        user_id = callback.from_user.id
        user_data = memoir_manager.get_user_data(user_id)
        current_chapter = user_data['current_chapter']
        
        # Получаем все записи главы
        memoirs = memoir_manager.get_chapter_memoirs_list(user_id, current_chapter)
        
        if not memoirs:
            await callback.message.answer("❌ В этой главе пока нет записей для навигации.")
            return
        
        # Показываем первую запись
        await show_memoir_by_index(callback.message, user_id, current_chapter, 0, memoirs)
        await state.set_state(MemoirStates.navigating_memoirs)
        
    except Exception as e:
        logger.error(f"Error starting memoir navigation: {e}")
        await callback.message.answer("❌ Ошибка при навигации по записям")

@router.callback_query(F.data.startswith("nav_"), StateFilter(MemoirStates.navigating_memoirs))
async def callback_navigate_memoir(callback: types.CallbackQuery, state: FSMContext):
    """Навигация между записями"""
    await safe_answer_callback(callback)
    
    try:
        user_id = callback.from_user.id
        user_data = memoir_manager.get_user_data(user_id)
        current_chapter = user_data['current_chapter']
        
        # Получаем все записи
        memoirs = memoir_manager.get_chapter_memoirs_list(user_id, current_chapter)
        
        if not memoirs:
            await callback.message.answer("❌ В этой главе нет записей.")
            await state.set_state(MemoirStates.waiting_for_memoir)
            return
        
        # Определяем направление навигации
        data = callback.data
        if data.startswith("nav_prev_"):
            current_index = int(data.replace("nav_prev_", ""))
            new_index = current_index - 1
        elif data.startswith("nav_next_"):
            current_index = int(data.replace("nav_next_", ""))
            new_index = current_index + 1
        else:
            return
        
        # Показываем новую запись
        await callback.message.delete()  # Удаляем старое сообщение
        await show_memoir_by_index(callback.message, user_id, current_chapter, new_index, memoirs)
        
    except Exception as e:
        logger.error(f"Error navigating memoir: {e}")
        await callback.message.answer("❌ Ошибка при навигации")

@router.callback_query(F.data.startswith("delete_memoir_"), StateFilter(MemoirStates.navigating_memoirs))
async def callback_delete_current_memoir(callback: types.CallbackQuery, state: FSMContext):
    """Удаление текущей записи"""
    await safe_answer_callback(callback)
    
    try:
        user_id = callback.from_user.id
        user_data = memoir_manager.get_user_data(user_id)
        current_chapter = user_data['current_chapter']
        memoir_id = callback.data.replace("delete_memoir_", "")
        
        # Подтверждение удаления
        confirmation_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_delete_memoir_{memoir_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_delete_memoir")
            ]
        ])
        
        await callback.message.answer(
            f"⚠️ Вы уверены, что хотите удалить запись '{memoir_id}'?",
            reply_markup=confirmation_keyboard
        )
        
    except Exception as e:
        logger.error(f"Error in delete memoir: {e}")
        await callback.message.answer("❌ Ошибка при удалении записи")

@router.callback_query(F.data.startswith("confirm_delete_memoir_"), StateFilter(MemoirStates.navigating_memoirs))
async def callback_confirm_delete_memoir(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение удаления записи"""
    await safe_answer_callback(callback)
    
    try:
        user_id = callback.from_user.id
        user_data = memoir_manager.get_user_data(user_id)
        current_chapter = user_data['current_chapter']
        memoir_id = callback.data.replace("confirm_delete_memoir_", "")
        
        # Удаляем запись
        success = memoir_manager.delete_memoir_by_id(user_id, current_chapter, memoir_id)
        
        if success:
            await callback.message.answer(f"✅ Запись '{memoir_id}' удалена!")
            
            # Показываем следующую запись или возвращаем к навигации
            memoirs = memoir_manager.get_chapter_memoirs_list(user_id, current_chapter)
            if memoirs:
                await show_memoir_by_index(callback.message, user_id, current_chapter, 0, memoirs)
            else:
                await callback.message.answer("❌ В главе больше нет записей.")
                await state.set_state(MemoirStates.waiting_for_memoir)
                await callback.message.answer("🔙 Главное меню:", reply_markup=get_main_keyboard())
        else:
            await callback.message.answer("❌ Ошибка при удалении записи.")
            
    except Exception as e:
        logger.error(f"Error confirming delete memoir: {e}")
        await callback.message.answer("❌ Ошибка при удалении записи")

@router.callback_query(F.data == "cancel_delete_memoir", StateFilter(MemoirStates.navigating_memoirs))
async def callback_cancel_delete_memoir(callback: types.CallbackQuery, state: FSMContext):
    """Отмена удаления записи"""
    await safe_answer_callback(callback)
    await callback.message.answer("❌ Удаление отменено.")
    
    # Возвращаем к навигации
    user_id = callback.from_user.id
    user_data = memoir_manager.get_user_data(user_id)
    current_chapter = user_data['current_chapter']
    memoirs = memoir_manager.get_chapter_memoirs_list(user_id, current_chapter)
    
    if memoirs:
        await show_memoir_by_index(callback.message, user_id, current_chapter, 0, memoirs)

@router.callback_query(F.data == "back_to_chapter_view", StateFilter(MemoirStates.navigating_memoirs))
async def callback_back_to_chapter_view(callback: types.CallbackQuery, state: FSMContext):
    """Возврат к просмотру главы"""
    await safe_answer_callback(callback)
    await state.set_state(MemoirStates.waiting_for_memoir)
    
    user_data = memoir_manager.get_user_data(callback.from_user.id)
    await callback.message.answer(
        f"📖 Выберите вариант просмотра для главы '{user_data['current_chapter']}':",
        reply_markup=get_chapter_view_keyboard()
    )

@router.callback_query(F.data == "back_to_main")
async def callback_back_to_main(callback: types.CallbackQuery, state: FSMContext):
    await safe_answer_callback(callback)
    await state.set_state(MemoirStates.waiting_for_memoir)
    await callback.message.answer("🔙 Главное меню:", reply_markup=get_main_keyboard())

# Регистрация роутера
dp.include_router(router)

async def main():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Bot starting...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"Bot startup error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")