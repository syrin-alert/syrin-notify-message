import os
import re
import pika
import json
import logging
import requests

# Set log level to INFO
logging.basicConfig(level=logging.INFO)

# Disable debug logs from pika by setting it to WARNING or higher
logging.getLogger("pika").setLevel(logging.WARNING)

# Load RabbitMQ settings from environment variables
rabbitmq_host = os.getenv('RABBITMQ_HOST', '')
rabbitmq_port = int(os.getenv('RABBITMQ_PORT', 5672))
rabbitmq_vhost = os.getenv('RABBITMQ_VHOST', '')
rabbitmq_user = os.getenv('RABBITMQ_USER', '')
rabbitmq_pass = os.getenv('RABBITMQ_PASS', '')
# TTL and DLX settings for reprocessing
rabbitmq_ttl_dlx = int(os.getenv('RABBITMQ_TTL_DLX', 60000))  # 60 seconds TTL (60000 ms)

# webhook service notification
webhook_type = os.getenv('WEBHOOK_TYPE', 'apprise').lower()  # Default to 'apprise' if not set
webhook_base_url = os.getenv('WEBHOOK_BASE_URL', 'http://...')
webhook_port = int(os.getenv('WEBHOOK_PORT', 80))

def get_webhook_url():
    # Determine the full webhook URL based on the type
    if webhook_type == 'alertmanager':
        return f"{webhook_base_url}:{webhook_port}/api/v1/alerts"
    else:  # default to Apprise
        return f"{webhook_base_url}:{webhook_port}/notify"


def format_message_for_telegram_markdown(message_text):
    formatted_lines = []
    for line in message_text.splitlines():
        # Verificar se a linha começa com o padrão [blabla-bla-bla] -
        if line.startswith('[') and '] - ' in line:
            # Extrair o conteúdo dentro dos colchetes e substituir os traços por espaços
            header_match = re.match(r'\[(.*?)\] -', line)
            if header_match:
                header_content = header_match.group(1).replace('-', ' ')
                # Colocar a primeira letra em maiúscula
                header_content = header_content[:1].upper() + header_content[1:]
                # Adicionar o conteúdo formatado como uma linha separada com negrito
                formatted_lines.append(f"*{header_content}*")
                # Remover o cabeçalho e continuar processando o restante da linha
                line = line.replace(header_match.group(0), '', 1).strip()

        # Verificar se a linha contém ":"
        if ':' in line:
            key, value = line.split(':', 1)
            # Escapar caracteres especiais e aplicar o formato de negrito
            formatted_key = re.sub(r'([_*[\]()~`>#+\-=|{}.!])', r'\\\1', key.strip())
            formatted_line = f"*{formatted_key}:* {value.strip()}"
        else:
            formatted_line = line

        formatted_lines.append(formatted_line)
    return '\n'.join(formatted_lines)


def prepare_payload(message):
    if webhook_type == 'alertmanager':
        # Alertmanager payload format
        humanized_text = message.get("humanized_text") or message.get("text", "No message provided")
        return [
            {
                "labels": {
                    "alertname": "NotificationAlert",
                    "severity": message.get("level", "info"),
                    "humanized_text": humanized_text,
                },
                "annotations": {
                    "description": humanized_text,
                }
            }
        ]
    else:
        # Apprise payload format, com formatação para o Telegram
        humanized_text = message.get("humanized_text")
        original_text = message.get("text", "No message provided")
        
        # Definir título e conteúdo com base na presença de "humanized_text"
        title = "AI Analyzed Notification" if humanized_text else "Notification Received"
        formatted_body = format_message_for_telegram_markdown(humanized_text or original_text)
        
        return {
            "title": title,
            "body": formatted_body
        }


def send_notification(message):
    try:
        # Get the appropriate webhook URL
        url = get_webhook_url()
        
        # Prepare payload based on the webhook type
        payload = prepare_payload(message)
        
        # Set headers for the request
        headers = {
            "Content-Type": "application/json"
        }
        
        # Log payload for debugging
        logging.info(f"[Syrin Notify Message] Sending payload to {webhook_type} webhook: {payload}")
        
        # Send the POST request to the webhook
        response = requests.post(url, json=payload, headers=headers)
        
        # Check if the request was successful
        if response.status_code == 200:
            logging.info(f"[Syrin Notify Message] Message successfully sent to {webhook_type} webhook: {message}")
            return True  # Indicates success
        else:
            logging.error(f"[Syrin Notify Message] Failed to send message to {webhook_type} webhook, status code: {response.status_code}")
            logging.error(f"[Syrin Notify Message] Response content: {response.text}")
            return False  # Indicates failure
    except requests.RequestException as e:
        logging.error(f"[Syrin Notify Message] Error sending message to {webhook_type} webhook: {str(e)}")
        return False  # Indicates failure


def publish_to_start_queue(channel, message):
    try:
        queue = '04_syrin_notification_message_process_send'
        channel.queue_declare(queue=queue, durable=True)
        channel.basic_publish(
            exchange='',
            routing_key=queue,
            body=json.dumps(message, ensure_ascii=False),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        logging.info(f"[Syrin Notify Message] Message published to queue {queue}: {message}")
    except Exception as e:
        logging.error(f"[Syrin Notify Message] Error publishing message to queue {queue}: {str(e)}")


def publish_to_reprocess_queue(channel, message):
    try:
        channel.queue_declare(
            queue='02_syrin_notification_message_reprocess_humanized',
            durable=True,
            arguments={
                'x-message-ttl': rabbitmq_ttl_dlx,  # Configurable TTL (1 minute)
                'x-dead-letter-exchange': '',  # Default DLX to route to another queue
                'x-dead-letter-routing-key': '02_syrin_notification_message_process_humanized'
            }
        )
        channel.basic_publish(
            exchange='',
            routing_key='02_syrin_notification_message_reprocess_humanized',
            body=json.dumps(message, ensure_ascii=False),
            properties=pika.BasicProperties(delivery_mode=2)
        )
        logging.info(f"[Syrin Notify Message] Message sent to reprocessing queue: {message['humanized_text']}")
    except Exception as e:
        logging.error(f"[Syrin Notify Message] Error sending message to reprocessing queue: {str(e)}")


def connect_to_rabbitmq():
    try:
        credentials = pika.PlainCredentials(rabbitmq_user, rabbitmq_pass)
        client_properties = {
            "connection_name": "Syrin Notify Message Agent"
        }
        
        parameters = pika.ConnectionParameters(
            host=rabbitmq_host,
            port=rabbitmq_port,
            virtual_host=rabbitmq_vhost,
            credentials=credentials,
            client_properties=client_properties
        )
        
        return pika.BlockingConnection(parameters)
    except Exception as e:
        logging.error(f"[Syrin Notify Message] Error connecting to RabbitMQ: {str(e)}")
        return None


def on_message_callback(channel, method_frame, header_frame, body):
    try:
        message = json.loads(body.decode())
        
        # Priorizar "humanized_text", caso contrário, usar "original_text"
        humanized_text = message.get("humanized_text") or message.get("text", "No message provided")
        level = message.get("level", "info")
        
        logging.info(f"[Syrin Notify Message] Message received from queue {method_frame.routing_key}: {humanized_text}, Level: {level}")

        # Attempt to send the message to the webhook
        success = send_notification(message)

        # Route the message based on the result of send_notification
        if success:
            publish_to_start_queue(channel, message)
            logging.info(f"[Syrin Notify Message] Message successfully sent to start queue.")
        else:
            publish_to_reprocess_queue(channel, message)
            logging.info(f"[Syrin Notify Message] Message sent to reprocessing queue due to failure.")

        # Acknowledge the message after processing
        channel.basic_ack(method_frame.delivery_tag)

    except Exception as e:
        logging.error(f"[Syrin Notify Message] Error in callback processing message: {str(e)}")
        channel.basic_ack(method_frame.delivery_tag)


def consume_messages():
    try:
        connection = connect_to_rabbitmq()
        if connection is None:
            logging.error("[Syrin Notify Message] Connection to RabbitMQ failed. Shutting down the application.")
            return

        channel = connection.channel()

        # Declare the queues to ensure they exist
        queues_to_declare = [
            '02_syrin_notification_message_process_humanized',
            '02_syrin_notification_message_reprocess_humanized',
            '04_syrin_notification_message_process_send',
        ]

        for queue in queues_to_declare:
            channel.queue_declare(
                queue=queue, 
                durable=True,
                arguments={
                    'x-message-ttl': rabbitmq_ttl_dlx,
                    'x-dead-letter-exchange': '',
                    'x-dead-letter-routing-key': '02_syrin_notification_message_process_humanized'
                } if queue == '02_syrin_notification_message_reprocess_humanized' else None
            )
            logging.info(f"[Syrin Notify Message] Queue '{queue}' checked or created.")

        # Register the callback for the queue '02_syrin_notification_message_process_humanized'
        channel.basic_consume(queue='02_syrin_notification_message_process_humanized', on_message_callback=on_message_callback)

        logging.info("[Syrin Notify Message] Waiting for messages...")
        
        # Start consuming messages
        channel.start_consuming()
    except Exception as e:
        logging.error(f"[Syrin Notify Message] Error consuming messages: {str(e)}")
    finally:
        if connection and connection.is_open:
            connection.close()
            logging.info("[Syrin Notify Message] Connection to RabbitMQ closed.")


if __name__ == "__main__":
    try:
        logging.info("[Syrin Notify Message] Syrin Notify Message - started \o/")
        consume_messages()
    except Exception as e:
        logging.error(f"[Syrin Notify Message] Error running the application: {str(e)}")