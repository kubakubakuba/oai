import discord
import requests
import re
import json
import sys
import time
import threading
import queue
import asyncio

# Make sure we're starting with a model.json and an identity.json
if len(sys.argv) != 3:
    print("Usage: python discord_llama.py model.json wizard.json")
    print("Make sure you have the llama.cpp server running already and your model.json points to it.")
    print("You must also have a pre-configured bot in discord applications:")
    print("https://discord.com/developers/applications")
    sys.exit(1)

# Load the llm config from the json file provided on command line
model_file = sys.argv[1]
with open(model_file, 'r') as file:
    model = json.load(file)

# Load the identity from the json file provided on command line
bot_file = sys.argv[2]
with open(bot_file, 'r') as file:
    bot = json.load(file)

# Configure discord intent for chatting
intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

# Removes discord IDs from strings
def remove_id(text):
    return re.sub(r'<@\d+>', '', text)

def format_prompt(prompt, user, question, history):
    formatted_prompt = prompt.replace("{user}", user)
    formatted_prompt = formatted_prompt.replace("{question}", question)
    formatted_prompt = formatted_prompt.replace("{history}", history)
    return formatted_prompt

# LLM Responder class to handle LLM requests in a separate thread
class LLMResponder:
    def __init__(self):
        self.request_queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self.process_requests)
        self.worker_thread.daemon = True
        self.worker_thread.start()
        self.response_times = []
        self.avg_response_time = 2 * 60  # Default to 2 minutes

    def llm_response(self, question):
        formatted_prompt = model["prompt_format"].replace("{system}", bot["identity"])
        formatted_prompt = formatted_prompt.replace("{prompt}", remove_id(question))
        print("Prompt: " + formatted_prompt)
        api_data = {
            "prompt": formatted_prompt,
            "n_predict": bot["tokens"],
            "temperature": bot["temperature"],
            "stop": model["stop_tokens"],
            "tokens_cached": 0,
            "repeat_penalty": 1.2
        }

        retries = 5
        backoff_factor = 1
        while retries > 0:
            try:
                response = requests.post(model["llama_endpoint"], headers={"Content-Type": "application/json"}, json=api_data)
                json_output = response.json()
                output = json_output['content']
                break
            except:
                time.sleep(backoff_factor)
                backoff_factor *= 2
                retries -= 1
                output = "My AI model is not responding try again in a moment 🔥🐳"
                continue
        print("LLM: " + output)
        return output

    def process_requests(self):
        while True:
            message, msg_ref, prompt, start_time = self.request_queue.get()
            response = self.llm_response(prompt)
            end_time = time.time()
            response_time = end_time - start_time
            self.response_times.append(response_time)
            self.avg_response_time = sum(self.response_times) / len(self.response_times)
            asyncio.run_coroutine_threadsafe(msg_ref.edit(content=response[:2000]), client.loop)
            self.request_queue.task_done()

    def add_request(self, message, msg_ref, prompt):
        start_time = time.time()
        self.request_queue.put((message, msg_ref, prompt, start_time))

responder = LLMResponder()

@client.event
async def on_ready():
    print(f'Bot logged in as {client.user}')

@client.event
async def on_message(message):

    if message.author == client.user:
        return
    
    if client.user.mentioned_in(message):
        print(message.author.name + ": " + remove_id(message.content))
        print(client.user.mentioned_in(message))
        history_list = []
        channel_history = [user async for user in message.channel.history(limit=bot["history_lines"] + 1)]
        for history in channel_history:
            if remove_id(history.content) != remove_id(message.content):
                history_list.append(history.author.name + ": " + remove_id(history.content)[:300])

        #print(history_list)

        history_list.reverse()
        history_text = '\n'.join(history_list)

        prompt = format_prompt(bot["question_prompt"], message.author.name, remove_id(message.content), history_text)
        avg_time = responder.avg_response_time / 60  # Convert to minutes
        # Send an immediate response using reply
        response_message = await message.reply(f"Generating, wait a minute... (average time: {avg_time:.2f} minutes)")
        responder.add_request(message, response_message, prompt)

client.run(bot["discord_token"])
