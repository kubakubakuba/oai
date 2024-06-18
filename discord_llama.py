import discord
import re
import json
import sys
import time
import threading
import queue
import asyncio
import requests
from functools import partial as curry

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

class LLMResponder:
    def __init__(self, model, bot):
        self.model = model
        print(model)
        print(bot)
        self.bot = bot
        self.request_queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self.process_requests)
        self.worker_thread.daemon = True
        self.worker_thread.start()
        self.response_times = []
        self.avg_response_time = 2 * 60  # Default to 2 minutes

    def llm_response(self, question):
        formatted_prompt = self.model["prompt_format"].replace("{system}", self.bot["identity"])
        print("removedid: " + remove_id(question))
        formatted_prompt = formatted_prompt.replace("{prompt}", remove_id(question))
        print("Prompt: " + formatted_prompt)
        api_data = {
            "prompt": formatted_prompt,
            "n_predict": self.bot["tokens"],
            "temperature": self.bot["temperature"],
            "stop": self.model["stop_tokens"],
            "tokens_cached": 0,
            "repeat_penalty": 1.2
        }

        retries = 5
        backoff_factor = 1
        while retries > 0:
            try:
                response = requests.post(self.model["llama_endpoint"], headers={"Content-Type": "application/json"}, json=api_data)
                json_output = response.json()
                output = json_output['content']
                break
            except:
                time.sleep(backoff_factor)
                backoff_factor *= 2
                retries -= 1
                output = "My AI model is not responding, try again in a moment 🔥🐳"
                continue
        print("LLM: " + output)
        return output

    def process_requests(self):
        while True:
            prompt, callback, start_time = self.request_queue.get()
            response = self.llm_response(prompt)
            end_time = time.time()
            response_time = end_time - start_time
            self.response_times.append(response_time)
            self.avg_response_time = sum(self.response_times) / len(self.response_times)
            callback(response)
            self.request_queue.task_done()

    def add_request(self, prompt, callback):
        start_time = time.time()
        self.request_queue.put((prompt, callback, start_time))

class DiscordLLMResponder(LLMResponder):
    def __init__(self, model, bot, client):
        super().__init__(model, bot)
        self.client = client

    def add_discord_request(self, message, msg_ref, prompt):
        def callback(response):
            asyncio.run_coroutine_threadsafe(msg_ref.edit(content=response[:2000]), self.client.loop)

        self.add_request(prompt, callback)


class ChannelSummaryManager:
    def __init__(self, snapshot_interval, llm, snapshot_limit=1000):
        self.llm = llm
        self.channel_message_counts = {}
        self.channel_message_summaries = {}

        self.snapshot_interval = snapshot_interval
        self.snapshot_limit = snapshot_limit

    async def update_channel_summary(self, channel):
        channel_id = channel.id
        if channel_id not in self.channel_message_counts:
            self.channel_message_counts[channel_id] = 0

        if self.channel_message_counts[channel_id] % self.snapshot_interval == 0:
            await self.take_snapshot(channel)

        self.channel_message_counts[channel_id] += 1
    def get_channel_summary(self, channel):
        channel_id = channel.id
        if channel_id in self.channel_message_summaries:
            return self.channel_message_summaries[channel_id]
        else:
            return False

    def record_message(self, channel, result):
        print(f"Channel {channel.id} received message: {result}")
        self.channel_message_summaries[channel.id] = result

    async def take_snapshot(self, channel):
        channel_history = [msg async for msg in channel.history(limit=self.snapshot_limit)]
        history_list = [f"{msg.author.name}: {remove_id(msg.content)[:1000]}" for msg in channel_history]
        #filter messages with only whitespaces and empty messages
        history_list = [msg for msg in history_list if msg.strip()]
        
        history_list.reverse()
        summary = '\n'.join(history_list)
        summary = summary + "\nsupervizor: Summarize the conversation above, what is it about? What are you could tell to the relevant persons? What is your opininon about the conversation? How could you help them? Write down the people in the chat and create a short summary about them."

        #print(f"Snapshot for channel {channel.id}: {summary}")
        self.llm.add_request(summary, curry(self.record_message, channel))
        # Trigger your desired action here
        # For example, save the summary to a file or send it to an API
summarizer = LLMResponder(model, bot)

summary_manager = ChannelSummaryManager(10, summarizer)
responder = DiscordLLMResponder(model, bot, client)

@client.event
async def on_ready():
    print(f'Bot logged in as {client.user}')

@client.event
async def on_message(message):
    print("detecting message")
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

        history_list.reverse()
        history_text = '\n'.join(history_list)
        summary_whole = ""
        start_summary = "[Summary]\n"
        end_summary = "[End Summary]\n"
        summary_generated = summary_manager.get_channel_summary(message.channel)
        if summary_generated:
            summary_whole = start_summary + summary_generated + end_summary

        print("Summary: " + summary_whole)

        prompt = format_prompt(bot["question_prompt"], message.author.name, remove_id(message.content), summary_whole+history_text)
        avg_time = responder.avg_response_time / 60  # Convert to minutes
        # Send an immediate response using reply
        response_message = await message.reply(f"Generating, wait a minute... (average time: {avg_time:.2f} minutes)")
        responder.add_discord_request(message, response_message, prompt)
        
        # Update the channel summary
    
    await summary_manager.update_channel_summary(message.channel)

client.run(bot["discord_token"])
