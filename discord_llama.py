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
        #print(model)
        #print(bot)
        self.bot = bot
        self.request_queue = queue.Queue()
        self.worker_thread = threading.Thread(target=self.process_requests)
        self.worker_thread.daemon = True
        self.worker_thread.start()
        self.response_times = []
        self.avg_response_time = 2 * 60  # Default to 2 minutes

    def llm_response(self, question):
        formatted_prompt = self.model["prompt_format"].replace("{system}", self.bot["identity"])
        formatted_prompt = formatted_prompt.replace("{prompt}", remove_id(question))
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
        #print("LLM: " + output)
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
    def __init__(self, snapshot_interval, llm, client, summary_channel_id, snapshot_limit=1000):
        self.llm = llm
        self.client = client
        self.summary_channel_id = summary_channel_id
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
            summary = self.channel_message_summaries[channel_id]
            return summary
        else:
            print("No summary available for this channel.")
            return False

    async def send_summary_to_channel(self, summary, channel_id):
        summary_channel = self.client.get_channel(self.summary_channel_id)
        print("Sending summary to channel")
        print(summary_channel)
        print("name: " + summary_channel.name)
        channel_name = self.client.get_channel(channel_id).name
        if summary_channel:
            print(f"\n----- Channel Summary: {channel_name} -----\n{summary}\n----------------------------\n")
            await summary_channel.send(f"\n----- Channel Summary: {channel_name} -----\n{summary}\n----------------------------\n")
        else:
            print("No summary channel found.")
    def record_message(self, channel, result):
        self.channel_message_summaries[channel.id] = result
        print(f"Channel summary for {channel.id}: {result}")
        asyncio.run_coroutine_threadsafe(self.send_summary_to_channel(result, channel.id), self.client.loop)

    async def take_snapshot(self, channel):
        channel_history = [msg async for msg in channel.history(limit=self.snapshot_limit)]
        history_list = [f"{msg.author.name}: {remove_id(msg.content)[:1000]}" for msg in channel_history]
        people_in_chat = set()
        for msg in channel_history:
            people_in_chat.add(msg.author.name)
        people_in_chat = ', '.join(people_in_chat)
        history_list = [msg for msg in history_list if msg.strip()]
        print("Taking snapshot of channel history for channel: " + channel.name)
        history_list.reverse()
        summary = '\n'.join(history_list)
        summary = summary + "\nsupervizor: Create a summary of the conversation above, what is it about? What is your opininon about the conversation? How could you help them? Write a short summary about these people: " + people_in_chat + ". It is crutial to write about each and every one of them.\n"

        self.llm.add_request(summary, curry(self.record_message, channel))

responder = DiscordLLMResponder(model, bot, client)

summary_channel_id = 1252659790799306824  # Replace with your actual summary channel ID
summary_manager = ChannelSummaryManager(10, responder, client, summary_channel_id, 100)


@client.event
async def on_ready():
    print(f'Bot logged in as {client.user}')

@client.event
async def on_message(message):
    print("detecting message")
    if message.author == client.user:
        return
    
    if client.user.mentioned_in(message):
        #print(message.author.name + ": " + remove_id(message.content))
        #print(client.user.mentioned_in(message))
        
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

        #print("Summary: " + summary_whole)

        prompt = format_prompt(bot["question_prompt"], message.author.name, remove_id(message.content), summary_whole+history_text)
        avg_time = responder.avg_response_time / 60  # Convert to minutes
        # Send an immediate response using reply
        response_message = await message.reply(f"Generating, wait a minute... (average time: {avg_time:.2f} minutes)")
        responder.add_discord_request(message, response_message, prompt)
        
        # Update the channel summary
    
    await summary_manager.update_channel_summary(message.channel)

client.run(bot["discord_token"])
