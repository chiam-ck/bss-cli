//! RabbitMQ plumbing — lapin binding for the `bss.events` topic exchange.
//!
//! Port of the aio-pika wiring the Python services share (declare the durable
//! `bss.events` topic exchange; publish persistent JSON; declare+bind a durable
//! queue and consume). lapin runs its own async-io reactor; the `tokio-*-trait`
//! shims host it on the tokio runtime so there's a single reactor (matching the
//! services' single-loop posture).
//!
//! This is the minimal surface Phase 1 (rating) needs: connect, `publish_json`
//! (the inline-publish path rating's consumer uses — no `message_id`, exactly
//! like `events/publisher.publish`), and `declare_and_bind` returning a raw
//! [`lapin::Consumer`] the service drives. The relay tick loop (only
//! subscription/com/som run it) lands in P2/P3 where a service exercises it.

use lapin::{
    options::{
        BasicConsumeOptions, BasicPublishOptions, BasicQosOptions, ExchangeDeclareOptions,
        QueueBindOptions, QueueDeclareOptions,
    },
    types::{AMQPValue, FieldTable, LongString, ShortString},
    BasicProperties, Channel, Connection, ConnectionProperties, Consumer, ExchangeKind,
};
use serde_json::Value;

use crate::topology::{parked_queue_name, retry_queue_name, EXCHANGE_NAME, RETRY_EXCHANGE_NAME};

/// Normalize the AMQP vhost to match aio-pika semantics: a URL ending in a bare
/// `/` carries an *empty* vhost, which lapin rejects (`vhost  not found`), but
/// aio-pika (and thus the running broker's config) treats it as the default
/// vhost `/`. Rewrite that one case to the URL-encoded default `%2f`; a URL with
/// an explicit vhost (`.../myvhost`) or none at all (`amqp://host`) is untouched.
fn normalize_vhost(mq_url: &str) -> String {
    if mq_url.ends_with('/') {
        format!("{mq_url}%2f")
    } else {
        mq_url.to_string()
    }
}

/// A live channel onto the `bss.events` topic exchange. Holds the connection so
/// the channel stays open for the lifetime of the value.
pub struct MqChannel {
    channel: Channel,
    _connection: Connection,
}

impl MqChannel {
    /// Connect to `mq_url`, open a channel, set prefetch to 5 (the services'
    /// `set_qos(prefetch_count=5)`), and declare the durable `bss.events` topic
    /// exchange. Returns the channel handle.
    pub async fn connect(mq_url: &str) -> Result<Self, lapin::Error> {
        let props = ConnectionProperties::default()
            .with_executor(tokio_executor_trait::Tokio::current())
            .with_reactor(tokio_reactor_trait::Tokio);
        let connection = Connection::connect(&normalize_vhost(mq_url), props).await?;
        let channel = connection.create_channel().await?;
        channel.basic_qos(5, BasicQosOptions::default()).await?;
        channel
            .exchange_declare(
                EXCHANGE_NAME,
                ExchangeKind::Topic,
                ExchangeDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;
        Ok(MqChannel {
            channel,
            _connection: connection,
        })
    }

    /// Publish `payload` as a persistent JSON message with routing key
    /// `routing_key`. Mirrors the inline `events/publisher.publish` message:
    /// `content_type=application/json`, persistent delivery, no `message_id`.
    pub async fn publish_json(
        &self,
        routing_key: &str,
        payload: &Value,
    ) -> Result<(), lapin::Error> {
        let body = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
        self.channel
            .basic_publish(
                EXCHANGE_NAME,
                routing_key,
                BasicPublishOptions::default(),
                &body,
                BasicProperties::default()
                    .with_content_type("application/json".into())
                    .with_delivery_mode(2), // persistent
            )
            .await?
            .await?; // await the broker confirm/return
        Ok(())
    }

    /// Declare a durable `queue`, bind it to `routing_key` on `bss.events`, and
    /// start consuming. The caller drives the returned [`Consumer`] stream and
    /// acks each delivery (rating acks unconditionally — it catches its own
    /// handler errors, never requeues).
    pub async fn declare_and_bind(
        &self,
        queue: &str,
        routing_key: &str,
        consumer_tag: &str,
    ) -> Result<Consumer, lapin::Error> {
        self.channel
            .queue_declare(
                queue,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;
        self.channel
            .queue_bind(
                queue,
                EXCHANGE_NAME,
                routing_key,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await?;
        self.channel
            .basic_consume(
                queue,
                consumer_tag,
                BasicConsumeOptions::default(),
                FieldTable::default(),
            )
            .await
    }

    /// Borrow the underlying channel (for ack/nack on deliveries).
    pub fn channel(&self) -> &Channel {
        &self.channel
    }

    /// Publish `payload` with an explicit AMQP `message_id` — the relay path (the
    /// inbox dedups consumers on this id). Otherwise identical to
    /// [`MqChannel::publish_json`].
    pub async fn publish_json_with_id(
        &self,
        routing_key: &str,
        payload: &Value,
        message_id: &str,
    ) -> Result<(), lapin::Error> {
        let body = serde_json::to_vec(payload).unwrap_or_else(|_| b"{}".to_vec());
        self.publish_bytes_with_id(routing_key, &body, message_id)
            .await
    }

    /// Publish pre-serialized `body` bytes with an AMQP `message_id` — the relay's
    /// drain path (it already serialized the payload).
    pub async fn publish_bytes_with_id(
        &self,
        routing_key: &str,
        body: &[u8],
        message_id: &str,
    ) -> Result<(), lapin::Error> {
        self.channel
            .basic_publish(
                EXCHANGE_NAME,
                routing_key,
                BasicPublishOptions::default(),
                body,
                BasicProperties::default()
                    .with_content_type("application/json".into())
                    .with_delivery_mode(2)
                    .with_message_id(ShortString::from(message_id)),
            )
            .await?
            .await?;
        Ok(())
    }

    /// Declare the shared retry (dead-letter) exchange — a durable direct
    /// exchange. Idempotent (port of `declare_retry_exchange`).
    pub async fn declare_retry_exchange(&self) -> Result<(), lapin::Error> {
        self.channel
            .exchange_declare(
                RETRY_EXCHANGE_NAME,
                ExchangeKind::Direct,
                ExchangeDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await
    }

    /// Declare the main + retry + parked topology for `queue_name` and start
    /// consuming the main queue — the lapin half of `bind_consumer`. The main
    /// queue dead-letters to the retry exchange (keyed by its own name); the retry
    /// queue holds the message for `retry_backoff_ms` then dead-letters it back to
    /// the main exchange under `routing_key`; the parked queue is the terminal
    /// resting place. Arg *types* mirror aio-pika (TTL as an integer, DLX names as
    /// strings) so a Rust and a Python service can share the durable queues.
    pub async fn bind_safe_consumer(
        &self,
        queue_name: &str,
        routing_key: &str,
        consumer_tag: &str,
        retry_backoff_ms: u64,
    ) -> Result<Consumer, lapin::Error> {
        // Main queue → retry exchange on failure.
        let mut main_args = FieldTable::default();
        main_args.insert(
            "x-dead-letter-exchange".into(),
            AMQPValue::LongString(LongString::from(RETRY_EXCHANGE_NAME)),
        );
        main_args.insert(
            "x-dead-letter-routing-key".into(),
            AMQPValue::LongString(LongString::from(queue_name)),
        );
        self.channel
            .queue_declare(
                queue_name,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                main_args,
            )
            .await?;
        self.channel
            .queue_bind(
                queue_name,
                EXCHANGE_NAME,
                routing_key,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await?;

        // Retry queue: TTL then dead-letter back to the main exchange/routing key.
        let retry_q = retry_queue_name(queue_name);
        let mut retry_args = FieldTable::default();
        retry_args.insert(
            "x-message-ttl".into(),
            AMQPValue::LongLongInt(retry_backoff_ms as i64),
        );
        retry_args.insert(
            "x-dead-letter-exchange".into(),
            AMQPValue::LongString(LongString::from(EXCHANGE_NAME)),
        );
        retry_args.insert(
            "x-dead-letter-routing-key".into(),
            AMQPValue::LongString(LongString::from(routing_key)),
        );
        self.channel
            .queue_declare(
                &retry_q,
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                retry_args,
            )
            .await?;
        self.channel
            .queue_bind(
                &retry_q,
                RETRY_EXCHANGE_NAME,
                queue_name,
                QueueBindOptions::default(),
                FieldTable::default(),
            )
            .await?;

        // Parked queue: terminal resting place for poison messages.
        self.channel
            .queue_declare(
                &parked_queue_name(queue_name),
                QueueDeclareOptions {
                    durable: true,
                    ..Default::default()
                },
                FieldTable::default(),
            )
            .await?;

        self.channel
            .basic_consume(
                queue_name,
                consumer_tag,
                BasicConsumeOptions::default(),
                FieldTable::default(),
            )
            .await
    }

    /// Park a poison message: publish it to `<queue_name>.parked` via the default
    /// exchange, carrying the failure reason. Mirrors the Python park publish.
    pub async fn publish_parked(
        &self,
        queue_name: &str,
        body: &[u8],
        message_id: Option<&str>,
        reason: &str,
    ) -> Result<(), lapin::Error> {
        let parked = parked_queue_name(queue_name);
        let mut headers = FieldTable::default();
        headers.insert(
            "parked_reason".into(),
            AMQPValue::LongString(LongString::from(truncate(reason, 500))),
        );
        let mut props = BasicProperties::default()
            .with_content_type("application/json".into())
            .with_delivery_mode(2)
            .with_headers(headers);
        if let Some(id) = message_id {
            props = props.with_message_id(ShortString::from(id));
        }
        // Default (nameless) exchange routes by queue name.
        self.channel
            .basic_publish("", &parked, BasicPublishOptions::default(), body, props)
            .await?
            .await?;
        Ok(())
    }
}

fn truncate(s: &str, max: usize) -> String {
    s.chars().take(max).collect()
}
