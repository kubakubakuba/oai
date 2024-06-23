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
from time import sleep
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
        self.bot = bot
        self.request_queue = queue.PriorityQueue()
        self.worker_thread = threading.Thread(target=self.process_requests)
        self.worker_thread.daemon = True
        self.worker_thread.start()
        self.response_times = []
        self.avg_response_time = 2 * 60  # Default to 2 minutes

    def llm_response(self, question, role="user", n_predict=None):
        formatted_prompt = self.model["prompt_format"].replace("{system}", self.bot["identity"])
        formatted_prompt = formatted_prompt.replace("{prompt}", remove_id(question))
        formatted_prompt = formatted_prompt.replace("{user}", role)  # Ensure this matches the placeholder in your prompt format
        print(f"Formatted prompt: {formatted_prompt}")
        # Use n_predict from the method argument if provided, otherwise use the default from self.bot
        tokens_to_generate = n_predict if n_predict is not None else self.bot["tokens"]
        api_data = {
            "prompt": formatted_prompt,
            "n_predict": tokens_to_generate,
            "temperature": self.bot["temperature"],
            "stop": self.model["stop_tokens"],
            "tokens_cached": 0,
            "repeat_penalty": 1.2,
            "penalize_nl": False,
            "top_p": 0.9
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
        return output

    def process_requests(self):
        while True:
            _, prompt, callback, start_time, role, n_predict = self.request_queue.get()  # Adjust to unpack n_predict
            print(f"Processing request with role: {role} and n_predict: {n_predict}")
            response = self.llm_response(prompt, role, n_predict)  # Pass n_predict to llm_response
            end_time = time.time()
            response_time = end_time - start_time
            self.response_times.append(response_time)
            self.avg_response_time = sum(self.response_times) / len(self.response_times)
            callback(response)
            self.request_queue.task_done()

    def add_request(self, prompt, callback, priority=False, role="user", n_predict=None):
        start_time = time.time()
        # Include n_predict in the queue
        self.request_queue.put((0 if priority else 1, prompt, callback, start_time, role, n_predict))

class DiscordLLMResponder(LLMResponder):
    def __init__(self, model, bot, client):
        super().__init__(model, bot)
        self.client = client

    def add_discord_request(self, message, msg_ref, prompt, role="user"):
        def callback(response):
            asyncio.run_coroutine_threadsafe(msg_ref.edit(content=response[:2000]), self.client.loop)

        self.add_request(prompt, callback, role=role)  # Pass role to add_request

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
            # Split the summary into chunks of 2000 characters or less
            tosplitLen = 1800
            chunks = [summary[i:i + tosplitLen] for i in range(0, len(summary), tosplitLen)]
            first = True
            for i,chunk in enumerate(chunks):
                

                print(f"Sending chunk: {chunk}")
                if first:
                    await summary_channel.send(f"\n----- Channel Summary: {channel_name} -----\n{chunk}")
                    first = False
                else:
                    await summary_channel.send(f"{chunk}")
                if(i == len(chunks)-1):
                    await summary_channel.send(f"----------------------------\n")

                sleep(1)

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
        people_in_chat_list = [f"{person}:\n" for person in people_in_chat.split(', ')]
        listofpeople = ''.join(people_in_chat_list)
        summary = f"\nCreate a summary of the following conversation, what is it about? What is your opininon about the conversation? How could you help them? Write a short summary about these people: \n {people_in_chat}. It is crutial to write about each and every one of them.\n[Start to summarize]{summary}[End to summarize]\n Remember to write a summary about the people in the conversation and the conversation itself. \n Use format like this: \n Summary: \n (long summary of what is generaly being discussed, should be atleast 15 sentences) \n People: (description of every person in the chat) {listofpeople}\n opinion/suggestions: (what do you think about the conversation, what could be done to help them) \n" 

        self.llm.add_request(summary, curry(self.record_message, channel), role="supervizor", n_predict=1536)


responder = DiscordLLMResponder(model, bot, client)

summary_channel_id = 1252659790799306824  # Replace with your actual summary channel ID
summary_manager = ChannelSummaryManager(10, responder, client, summary_channel_id, 410)


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

        print("Summary: " + summary_whole)

        prompt = format_prompt(bot["question_prompt"], message.author.name, remove_id(message.content), summary_whole+history_text)
        avg_time = responder.avg_response_time / 60  # Convert to minutes
        # Send an immediate response using reply
        response_message = await message.reply(f"Generating, wait a minute... (average time: {avg_time:.2f} minutes)")
        responder.add_discord_request(message, response_message, prompt)
        
        # Update the channel summary
    
    await summary_manager.update_channel_summary(message.channel)

client.run(bot["discord_token"])
